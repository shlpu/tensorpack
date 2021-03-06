#!/usr/bin/env python
# -*- coding: utf-8 -*-
# File: train.py

import os
import argparse
import cv2
import shutil
import itertools
import tqdm
import numpy as np
import json
import six
import tensorflow as tf
try:
    import horovod.tensorflow as hvd
except ImportError:
    pass

assert six.PY3, "FasterRCNN requires Python 3!"

from tensorpack import *
from tensorpack.tfutils.summary import add_moving_summary
from tensorpack.tfutils.scope_utils import under_name_scope
from tensorpack.tfutils import optimizer
from tensorpack.tfutils.common import get_tf_version_number
import tensorpack.utils.viz as tpviz
from tensorpack.utils.gpu import get_num_gpu


from coco import COCODetection
from basemodel import (
    image_preprocess, resnet_c4_backbone, resnet_conv5,
    resnet_fpn_backbone)
from model import (
    clip_boxes, decode_bbox_target, encode_bbox_target, crop_and_resize,
    rpn_head, rpn_losses,
    generate_rpn_proposals, sample_fast_rcnn_targets, roi_align,
    fastrcnn_outputs, fastrcnn_losses, fastrcnn_predictions,
    maskrcnn_upXconv_head, maskrcnn_loss,
    fpn_model, fastrcnn_2fc_head, multilevel_roi_align)
from data import (
    get_train_dataflow, get_eval_dataflow,
    get_all_anchors, get_all_anchors_fpn)
from viz import (
    draw_annotation, draw_proposal_recall,
    draw_predictions, draw_final_outputs)
from common import print_config, write_config_from_args
from eval import (
    eval_coco, detect_one_image, print_evaluation_scores, DetectionResult)
import config


def get_model_output_names():
    ret = ['final_boxes', 'final_probs', 'final_labels']
    if config.MODE_MASK:
        ret.append('final_masks')
    return ret


def get_model():
    if config.MODE_FPN:
        if get_tf_version_number() < 1.6:
            logger.warn("FPN has chances to crash in TF<1.6, due to a TF issue.")
        return ResNetFPNModel()
    else:
        return ResNetC4Model()


class DetectionModel(ModelDesc):
    def preprocess(self, image):
        image = tf.expand_dims(image, 0)
        image = image_preprocess(image, bgr=True)
        return tf.transpose(image, [0, 3, 1, 2])

    @under_name_scope()
    def narrow_to_featuremap(self, featuremap, anchors, anchor_labels, anchor_boxes):
        """
        Slice anchors/anchor_labels/anchor_boxes to the spatial size of this featuremap.

        Args:
            anchors (FS x FS x NA x 4):
            anchor_labels (FS x FS x NA):
            anchor_boxes (FS x FS x NA x 4):
        """
        shape2d = tf.shape(featuremap)[2:]  # h,w
        slice3d = tf.concat([shape2d, [-1]], axis=0)
        slice4d = tf.concat([shape2d, [-1, -1]], axis=0)
        anchors = tf.slice(anchors, [0, 0, 0, 0], slice4d)
        anchor_labels = tf.slice(anchor_labels, [0, 0, 0], slice3d)
        anchor_boxes = tf.slice(anchor_boxes, [0, 0, 0, 0], slice4d)
        return anchors, anchor_labels, anchor_boxes

    def optimizer(self):
        lr = tf.get_variable('learning_rate', initializer=0.003, trainable=False)
        tf.summary.scalar('learning_rate-summary', lr)

        factor = config.NUM_GPUS / 8.
        if factor != 1:
            lr = lr * factor
        opt = tf.train.MomentumOptimizer(lr, 0.9)
        if config.NUM_GPUS < 8:
            opt = optimizer.AccumGradOptimizer(opt, 8 // config.NUM_GPUS)
        return opt

    def fastrcnn_training(self, image,
                          rcnn_labels, fg_rcnn_boxes, gt_boxes_per_fg,
                          rcnn_label_logits, fg_rcnn_box_logits):
        """
        Args:
            image (NCHW):
            rcnn_labels (n): labels for each sampled targets
            fg_rcnn_boxes (fg x 4): proposal boxes for each sampled foreground targets
            gt_boxes_per_fg (fg x 4): matching gt boxes for each sampled foreground targets
            rcnn_label_logits (n): label logits for each sampled targets
            fg_rcnn_box_logits (fg x #class x 4): box logits for each sampled foreground targets
        """

        with tf.name_scope('fg_sample_patch_viz'):
            fg_sampled_patches = crop_and_resize(
                image, fg_rcnn_boxes,
                tf.zeros(tf.shape(fg_rcnn_boxes)[0], dtype=tf.int32), 300)
            fg_sampled_patches = tf.transpose(fg_sampled_patches, [0, 2, 3, 1])
            fg_sampled_patches = tf.reverse(fg_sampled_patches, axis=[-1])  # BGR->RGB
            tf.summary.image('viz', fg_sampled_patches, max_outputs=30)

        encoded_boxes = encode_bbox_target(
            gt_boxes_per_fg, fg_rcnn_boxes) * tf.constant(config.FASTRCNN_BBOX_REG_WEIGHTS, dtype=tf.float32)
        fastrcnn_label_loss, fastrcnn_box_loss = fastrcnn_losses(
            rcnn_labels, rcnn_label_logits,
            encoded_boxes,
            fg_rcnn_box_logits)
        return fastrcnn_label_loss, fastrcnn_box_loss

    def fastrcnn_inference(self, image_shape2d,
                           rcnn_boxes, rcnn_label_logits, rcnn_box_logits):
        """
        Args:
            image_shape2d: h, w
            rcnn_boxes (nx4): the proposal boxes
            rcnn_label_logits (n):
            rcnn_box_logits (nx #class x 4):

        Returns:
            boxes (mx4):
            labels (m): each >= 1
        """
        label_probs = tf.nn.softmax(rcnn_label_logits, name='fastrcnn_all_probs')  # #proposal x #Class
        anchors = tf.tile(tf.expand_dims(rcnn_boxes, 1), [1, config.NUM_CLASS - 1, 1])   # #proposal x #Cat x 4
        decoded_boxes = decode_bbox_target(
            rcnn_box_logits /
            tf.constant(config.FASTRCNN_BBOX_REG_WEIGHTS, dtype=tf.float32), anchors)
        decoded_boxes = clip_boxes(decoded_boxes, image_shape2d, name='fastrcnn_all_boxes')

        # indices: Nx2. Each index into (#proposal, #category)
        pred_indices, final_probs = fastrcnn_predictions(decoded_boxes, label_probs)
        final_probs = tf.identity(final_probs, 'final_probs')
        final_boxes = tf.gather_nd(decoded_boxes, pred_indices, name='final_boxes')
        final_labels = tf.add(pred_indices[:, 1], 1, name='final_labels')
        return final_boxes, final_labels


class ResNetC4Model(DetectionModel):
    def inputs(self):
        ret = [
            tf.placeholder(tf.float32, (None, None, 3), 'image'),
            tf.placeholder(tf.int32, (None, None, config.NUM_ANCHOR), 'anchor_labels'),
            tf.placeholder(tf.float32, (None, None, config.NUM_ANCHOR, 4), 'anchor_boxes'),
            tf.placeholder(tf.float32, (None, 4), 'gt_boxes'),
            tf.placeholder(tf.int64, (None,), 'gt_labels')]  # all > 0
        if config.MODE_MASK:
            ret.append(
                tf.placeholder(tf.uint8, (None, None, None), 'gt_masks')
            )   # NR_GT x height x width
        return ret

    def build_graph(self, *inputs):
        is_training = get_current_tower_context().is_training
        if config.MODE_MASK:
            image, anchor_labels, anchor_boxes, gt_boxes, gt_labels, gt_masks = inputs
        else:
            image, anchor_labels, anchor_boxes, gt_boxes, gt_labels = inputs
        image = self.preprocess(image)     # 1CHW

        featuremap = resnet_c4_backbone(image, config.RESNET_NUM_BLOCK[:3])
        rpn_label_logits, rpn_box_logits = rpn_head('rpn', featuremap, 1024, config.NUM_ANCHOR)

        fm_anchors, anchor_labels, anchor_boxes = self.narrow_to_featuremap(
            featuremap, get_all_anchors(), anchor_labels, anchor_boxes)
        anchor_boxes_encoded = encode_bbox_target(anchor_boxes, fm_anchors)

        image_shape2d = tf.shape(image)[2:]     # h,w
        pred_boxes_decoded = decode_bbox_target(rpn_box_logits, fm_anchors)  # fHxfWxNAx4, floatbox
        proposal_boxes, proposal_scores = generate_rpn_proposals(
            tf.reshape(pred_boxes_decoded, [-1, 4]),
            tf.reshape(rpn_label_logits, [-1]),
            image_shape2d,
            config.TRAIN_PRE_NMS_TOPK if is_training else config.TEST_PRE_NMS_TOPK,
            config.TRAIN_POST_NMS_TOPK if is_training else config.TEST_POST_NMS_TOPK)

        if is_training:
            # sample proposal boxes in training
            rcnn_boxes, rcnn_labels, fg_inds_wrt_gt = sample_fast_rcnn_targets(
                proposal_boxes, gt_boxes, gt_labels)
        else:
            # The boxes to be used to crop RoIs.
            # Use all proposal boxes in inference
            rcnn_boxes = proposal_boxes

        boxes_on_featuremap = rcnn_boxes * (1.0 / config.ANCHOR_STRIDE)
        roi_resized = roi_align(featuremap, boxes_on_featuremap, 14)

        # HACK to work around https://github.com/tensorflow/tensorflow/issues/14657
        # which was fixed in TF 1.6
        def ff_true():
            feature_fastrcnn = resnet_conv5(roi_resized, config.RESNET_NUM_BLOCK[-1])    # nxcx7x7
            feature_gap = GlobalAvgPooling('gap', feature_fastrcnn, data_format='channels_first')
            fastrcnn_label_logits, fastrcnn_box_logits = fastrcnn_outputs('fastrcnn', feature_gap, config.NUM_CLASS)
            # Return C5 feature to be shared with mask branch
            return feature_fastrcnn, fastrcnn_label_logits, fastrcnn_box_logits

        def ff_false():
            ncls = config.NUM_CLASS
            return tf.zeros([0, 2048, 7, 7]), tf.zeros([0, ncls]), tf.zeros([0, ncls - 1, 4])

        if get_tf_version_number() >= 1.6:
            feature_fastrcnn, fastrcnn_label_logits, fastrcnn_box_logits = ff_true()
        else:
            logger.warn("This example may drop support for TF < 1.6 soon.")
            feature_fastrcnn, fastrcnn_label_logits, fastrcnn_box_logits = tf.cond(
                tf.size(boxes_on_featuremap) > 0, ff_true, ff_false)

        if is_training:
            # rpn loss
            rpn_label_loss, rpn_box_loss = rpn_losses(
                anchor_labels, anchor_boxes_encoded, rpn_label_logits, rpn_box_logits)

            # fastrcnn loss
            matched_gt_boxes = tf.gather(gt_boxes, fg_inds_wrt_gt)

            fg_inds_wrt_sample = tf.reshape(tf.where(rcnn_labels > 0), [-1])   # fg inds w.r.t all samples
            fg_sampled_boxes = tf.gather(rcnn_boxes, fg_inds_wrt_sample)
            fg_fastrcnn_box_logits = tf.gather(fastrcnn_box_logits, fg_inds_wrt_sample)

            fastrcnn_label_loss, fastrcnn_box_loss = self.fastrcnn_training(
                image, rcnn_labels, fg_sampled_boxes,
                matched_gt_boxes, fastrcnn_label_logits, fg_fastrcnn_box_logits)

            if config.MODE_MASK:
                # maskrcnn loss
                fg_labels = tf.gather(rcnn_labels, fg_inds_wrt_sample)
                # In training, mask branch shares the same C5 feature.
                fg_feature = tf.gather(feature_fastrcnn, fg_inds_wrt_sample)
                mask_logits = maskrcnn_upXconv_head(
                    'maskrcnn', fg_feature, config.NUM_CLASS, num_convs=0)   # #fg x #cat x 14x14

                target_masks_for_fg = crop_and_resize(
                    tf.expand_dims(gt_masks, 1),
                    fg_sampled_boxes,
                    fg_inds_wrt_gt, 14,
                    pad_border=False)  # nfg x 1x14x14
                target_masks_for_fg = tf.squeeze(target_masks_for_fg, 1, 'sampled_fg_mask_targets')
                mrcnn_loss = maskrcnn_loss(mask_logits, fg_labels, target_masks_for_fg)
            else:
                mrcnn_loss = 0.0

            wd_cost = regularize_cost(
                '(?:group1|group2|group3|rpn|fastrcnn|maskrcnn)/.*W',
                l2_regularizer(1e-4), name='wd_cost')

            total_cost = tf.add_n([
                rpn_label_loss, rpn_box_loss,
                fastrcnn_label_loss, fastrcnn_box_loss,
                mrcnn_loss,
                wd_cost], 'total_cost')

            add_moving_summary(total_cost, wd_cost)
            return total_cost
        else:
            final_boxes, final_labels = self.fastrcnn_inference(
                image_shape2d, rcnn_boxes, fastrcnn_label_logits, fastrcnn_box_logits)

            if config.MODE_MASK:
                # HACK to work around https://github.com/tensorflow/tensorflow/issues/14657
                def f1():
                    roi_resized = roi_align(featuremap, final_boxes * (1.0 / config.ANCHOR_STRIDE), 14)
                    feature_maskrcnn = resnet_conv5(roi_resized, config.RESNET_NUM_BLOCK[-1])
                    mask_logits = maskrcnn_upXconv_head(
                        'maskrcnn', feature_maskrcnn, config.NUM_CLASS, 0)   # #result x #cat x 14x14
                    indices = tf.stack([tf.range(tf.size(final_labels)), tf.to_int32(final_labels) - 1], axis=1)
                    final_mask_logits = tf.gather_nd(mask_logits, indices)   # #resultx14x14
                    return tf.sigmoid(final_mask_logits)

                final_masks = tf.cond(tf.size(final_labels) > 0, f1, lambda: tf.zeros([0, 14, 14]))
                tf.identity(final_masks, name='final_masks')


class ResNetFPNModel(DetectionModel):
    def inputs(self):
        ret = [
            tf.placeholder(tf.float32, (None, None, 3), 'image')]
        num_anchors = len(config.ANCHOR_RATIOS)
        for k in range(len(config.ANCHOR_STRIDES_FPN)):
            ret.extend([
                tf.placeholder(tf.int32, (None, None, num_anchors),
                               'anchor_labels_lvl{}'.format(k + 2)),
                tf.placeholder(tf.float32, (None, None, num_anchors, 4),
                               'anchor_boxes_lvl{}'.format(k + 2))])
        ret.extend([
            tf.placeholder(tf.float32, (None, 4), 'gt_boxes'),
            tf.placeholder(tf.int64, (None,), 'gt_labels')])  # all > 0
        if config.MODE_MASK:
            ret.append(
                tf.placeholder(tf.uint8, (None, None, None), 'gt_masks')
            )   # NR_GT x height x width
        return ret

    def build_graph(self, *inputs):
        num_fpn_level = len(config.ANCHOR_STRIDES_FPN)
        assert len(config.ANCHOR_SIZES) == num_fpn_level
        is_training = get_current_tower_context().is_training
        image = inputs[0]
        input_anchors = inputs[1: 1 + 2 * num_fpn_level]
        multilevel_anchor_labels = input_anchors[0::2]
        multilevel_anchor_boxes = input_anchors[1::2]
        gt_boxes, gt_labels = inputs[11], inputs[12]
        if config.MODE_MASK:
            gt_masks = inputs[-1]

        image = self.preprocess(image)     # 1CHW
        image_shape2d = tf.shape(image)[2:]     # h,w

        c2345 = resnet_fpn_backbone(image, config.RESNET_NUM_BLOCK)
        p23456 = fpn_model('fpn', c2345)

        # Images are padded for p5, which are too large for p2-p4.
        # This seems to have no effect on mAP.
        for i, stride in enumerate(config.ANCHOR_STRIDES_FPN[:3]):
            pi = p23456[i]
            target_shape = tf.to_int32(tf.ceil(tf.to_float(image_shape2d) * (1.0 / stride)))
            p23456[i] = tf.slice(pi, [0, 0, 0, 0],
                                 tf.concat([[-1, -1], target_shape], axis=0))
            p23456[i].set_shape([1, pi.shape[1], None, None])

        # Multi-Level RPN Proposals
        multilevel_proposals = []
        rpn_loss_collection = []
        for lvl in range(num_fpn_level):
            rpn_label_logits, rpn_box_logits = rpn_head(
                'rpn', p23456[lvl], config.FPN_NUM_CHANNEL, len(config.ANCHOR_RATIOS))
            with tf.name_scope('FPN_lvl{}'.format(lvl + 2)):
                anchors = tf.constant(get_all_anchors_fpn()[lvl], name='rpn_anchor_lvl{}'.format(lvl + 2))
                anchors, anchor_labels, anchor_boxes = \
                    self.narrow_to_featuremap(p23456[lvl], anchors,
                                              multilevel_anchor_labels[lvl],
                                              multilevel_anchor_boxes[lvl])
                anchor_boxes_encoded = encode_bbox_target(anchor_boxes, anchors)
                pred_boxes_decoded = decode_bbox_target(rpn_box_logits, anchors)
                proposal_boxes, proposal_scores = generate_rpn_proposals(
                    tf.reshape(pred_boxes_decoded, [-1, 4]),
                    tf.reshape(rpn_label_logits, [-1]),
                    image_shape2d,
                    config.TRAIN_FPN_NMS_TOPK if is_training else config.TEST_FPN_NMS_TOPK)
                multilevel_proposals.append((proposal_boxes, proposal_scores))
                if is_training:
                    label_loss, box_loss = rpn_losses(
                        anchor_labels, anchor_boxes_encoded,
                        rpn_label_logits, rpn_box_logits)
                    rpn_loss_collection.extend([label_loss, box_loss])

        # Merge proposals from multi levels, pick top K
        proposal_boxes = tf.concat([x[0] for x in multilevel_proposals], axis=0)  # nx4
        proposal_scores = tf.concat([x[1] for x in multilevel_proposals], axis=0)  # n
        proposal_topk = tf.minimum(tf.size(proposal_scores),
                                   config.TRAIN_FPN_NMS_TOPK if is_training else config.TEST_FPN_NMS_TOPK)
        proposal_scores, topk_indices = tf.nn.top_k(proposal_scores, k=proposal_topk, sorted=False)
        proposal_boxes = tf.gather(proposal_boxes, topk_indices)

        if is_training:
            rcnn_boxes, rcnn_labels, fg_inds_wrt_gt = sample_fast_rcnn_targets(
                proposal_boxes, gt_boxes, gt_labels)
        else:
            # The boxes to be used to crop RoIs.
            rcnn_boxes = proposal_boxes

        roi_feature_fastrcnn = multilevel_roi_align(p23456[:4], rcnn_boxes, 7)

        fastrcnn_label_logits, fastrcnn_box_logits = fastrcnn_2fc_head(
            'fastrcnn', roi_feature_fastrcnn, config.NUM_CLASS)

        if is_training:
            # rpn loss is already defined above
            with tf.name_scope('rpn_losses'):
                rpn_total_label_loss = tf.add_n(rpn_loss_collection[::2], name='label_loss')
                rpn_total_box_loss = tf.add_n(rpn_loss_collection[1::2], name='box_loss')
                add_moving_summary(rpn_total_box_loss, rpn_total_label_loss)

            # fastrcnn loss:
            matched_gt_boxes = tf.gather(gt_boxes, fg_inds_wrt_gt)

            fg_inds_wrt_sample = tf.reshape(tf.where(rcnn_labels > 0), [-1])   # fg inds w.r.t all samples
            fg_sampled_boxes = tf.gather(rcnn_boxes, fg_inds_wrt_sample)
            fg_fastrcnn_box_logits = tf.gather(fastrcnn_box_logits, fg_inds_wrt_sample)

            fastrcnn_label_loss, fastrcnn_box_loss = self.fastrcnn_training(
                image, rcnn_labels, fg_sampled_boxes,
                matched_gt_boxes, fastrcnn_label_logits, fg_fastrcnn_box_logits)

            if config.MODE_MASK:
                # maskrcnn loss
                fg_labels = tf.gather(rcnn_labels, fg_inds_wrt_sample)
                roi_feature_maskrcnn = multilevel_roi_align(
                    p23456[:4], fg_sampled_boxes, 14)
                mask_logits = maskrcnn_upXconv_head(
                    'maskrcnn', roi_feature_maskrcnn, config.NUM_CLASS, 4)   # #fg x #cat x 28 x 28

                target_masks_for_fg = crop_and_resize(
                    tf.expand_dims(gt_masks, 1),
                    fg_sampled_boxes,
                    fg_inds_wrt_gt, 28,
                    pad_border=False)  # fg x 1x28x28
                target_masks_for_fg = tf.squeeze(target_masks_for_fg, 1, 'sampled_fg_mask_targets')
                mrcnn_loss = maskrcnn_loss(mask_logits, fg_labels, target_masks_for_fg)
            else:
                mrcnn_loss = 0.0

            wd_cost = regularize_cost(
                '(?:group1|group2|group3|rpn|fpn|fastrcnn|maskrcnn)/.*W',
                l2_regularizer(1e-4), name='wd_cost')

            total_cost = tf.add_n(rpn_loss_collection + [
                fastrcnn_label_loss, fastrcnn_box_loss,
                mrcnn_loss, wd_cost], 'total_cost')

            add_moving_summary(total_cost, wd_cost)
            return total_cost
        else:
            final_boxes, final_labels = self.fastrcnn_inference(
                image_shape2d, rcnn_boxes, fastrcnn_label_logits, fastrcnn_box_logits)
            if config.MODE_MASK:
                # Cascade inference needs roi transform with refined boxes.
                roi_feature_maskrcnn = multilevel_roi_align(p23456[:4], final_boxes, 14)
                mask_logits = maskrcnn_upXconv_head(
                    'maskrcnn', roi_feature_maskrcnn, config.NUM_CLASS, 4)   # #fg x #cat x 28 x 28
                indices = tf.stack([tf.range(tf.size(final_labels)), tf.to_int32(final_labels) - 1], axis=1)
                final_mask_logits = tf.gather_nd(mask_logits, indices)   # #resultx28x28
                tf.sigmoid(final_mask_logits, name='final_masks')


def visualize(model_path, nr_visualize=50, output_dir='output'):
    """
    Visualize some intermediate results (proposals, raw predictions) inside the pipeline.
    Does not support FPN.
    """
    df = get_train_dataflow()   # we don't visualize mask stuff
    df.reset_state()

    pred = OfflinePredictor(PredictConfig(
        model=ResNetC4Model(),
        session_init=get_model_loader(model_path),
        input_names=['image', 'gt_boxes', 'gt_labels'],
        output_names=[
            'generate_rpn_proposals/boxes',
            'generate_rpn_proposals/probs',
            'fastrcnn_all_probs',
            'final_boxes',
            'final_probs',
            'final_labels',
        ]))

    if os.path.isdir(output_dir):
        shutil.rmtree(output_dir)
    utils.fs.mkdir_p(output_dir)
    with tqdm.tqdm(total=nr_visualize) as pbar:
        for idx, dp in itertools.islice(enumerate(df.get_data()), nr_visualize):
            img, _, _, gt_boxes, gt_labels = dp

            rpn_boxes, rpn_scores, all_probs, \
                final_boxes, final_probs, final_labels = pred(img, gt_boxes, gt_labels)

            # draw groundtruth boxes
            gt_viz = draw_annotation(img, gt_boxes, gt_labels)
            # draw best proposals for each groundtruth, to show recall
            proposal_viz, good_proposals_ind = draw_proposal_recall(img, rpn_boxes, rpn_scores, gt_boxes)
            # draw the scores for the above proposals
            score_viz = draw_predictions(img, rpn_boxes[good_proposals_ind], all_probs[good_proposals_ind])

            results = [DetectionResult(*args) for args in
                       zip(final_boxes, final_probs, final_labels,
                           [None] * len(final_labels))]
            final_viz = draw_final_outputs(img, results)

            viz = tpviz.stack_patches([
                gt_viz, proposal_viz,
                score_viz, final_viz], 2, 2)

            if os.environ.get('DISPLAY', None):
                tpviz.interactive_imshow(viz)
            cv2.imwrite("{}/{:03d}.png".format(output_dir, idx), viz)
            pbar.update()


def offline_evaluate(pred_func, output_file):
    df = get_eval_dataflow()
    all_results = eval_coco(
        df, lambda img: detect_one_image(img, pred_func))
    with open(output_file, 'w') as f:
        json.dump(all_results, f)
    print_evaluation_scores(output_file)


def predict(pred_func, input_file):
    img = cv2.imread(input_file, cv2.IMREAD_COLOR)
    results = detect_one_image(img, pred_func)
    final = draw_final_outputs(img, results)
    viz = np.concatenate((img, final), axis=1)
    tpviz.interactive_imshow(viz)


class EvalCallback(Callback):
    def _setup_graph(self):
        self.pred = self.trainer.get_predictor(
            ['image'], get_model_output_names())
        self.df = get_eval_dataflow()

    def _before_train(self):
        EVAL_TIMES = 5  # eval 5 times during training
        interval = self.trainer.max_epoch // (EVAL_TIMES + 1)
        self.epochs_to_eval = set([interval * k for k in range(1, EVAL_TIMES + 1)])
        self.epochs_to_eval.add(self.trainer.max_epoch)
        logger.info("[EvalCallback] Will evaluate at epoch " + str(sorted(self.epochs_to_eval)))

    def _eval(self):
        all_results = eval_coco(self.df, lambda img: detect_one_image(img, self.pred))
        output_file = os.path.join(
            logger.get_logger_dir(), 'outputs{}.json'.format(self.global_step))
        with open(output_file, 'w') as f:
            json.dump(all_results, f)
        try:
            scores = print_evaluation_scores(output_file)
        except Exception:
            logger.exception("Exception in COCO evaluation.")
            scores = {}
        for k, v in scores.items():
            self.trainer.monitors.put_scalar(k, v)

    def _trigger_epoch(self):
        if self.epoch_num in self.epochs_to_eval:
            self._eval()


def init_config():
    if config.TRAINER == 'horovod':
        ngpu = hvd.size()
    else:
        ngpu = get_num_gpu()
    assert ngpu % 8 == 0 or 8 % ngpu == 0, ngpu
    if config.NUM_GPUS is None:
        config.NUM_GPUS = ngpu
    else:
        if config.TRAINER == 'horovod':
            assert config.NUM_GPUS == ngpu
        else:
            assert config.NUM_GPUS <= ngpu
    print_config()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--load', help='load a model for evaluation or training')
    parser.add_argument('--logdir', help='log directory', default='train_log/maskrcnn')
    parser.add_argument('--visualize', action='store_true', help='visualize intermediate results')
    parser.add_argument('--evaluate', help="Run evaluation on COCO. "
                                           "This argument is the path to the output json evaluation file")
    parser.add_argument('--predict', help="Run prediction on a given image. "
                                          "This argument is the path to the input image file")
    parser.add_argument('--config', help="A list of key=value to overwrite those defined in config.py",
                        nargs='+')

    args = parser.parse_args()
    write_config_from_args(args.config)

    if args.visualize or args.evaluate or args.predict:
        # autotune is too slow for inference
        os.environ['TF_CUDNN_USE_AUTOTUNE'] = '0'

        assert args.load
        print_config()

        if args.predict or args.visualize:
            config.RESULT_SCORE_THRESH = config.RESULT_SCORE_THRESH_VIS

        if args.visualize:
            assert not config.MODE_FPN, "FPN visualize is not supported!"
            visualize(args.load)
        else:
            pred = OfflinePredictor(PredictConfig(
                model=get_model(),
                session_init=get_model_loader(args.load),
                input_names=['image'],
                output_names=get_model_output_names()))
            if args.evaluate:
                assert args.evaluate.endswith('.json')
                offline_evaluate(pred, args.evaluate)
            elif args.predict:
                COCODetection(config.BASEDIR, 'val2014')   # Only to load the class names into caches
                predict(pred, args.predict)
    else:
        os.environ['TF_AUTOTUNE_THRESHOLD'] = '1'
        is_horovod = config.TRAINER == 'horovod'
        if is_horovod:
            hvd.init()
            logger.info("Horovod Rank={}, Size={}".format(hvd.rank(), hvd.size()))
        else:
            assert 'OMPI_COMM_WORLD_SIZE' not in os.environ

        if not is_horovod or hvd.rank() == 0:
            logger.set_logger_dir(args.logdir, 'd')

        init_config()
        factor = 8. / config.NUM_GPUS
        stepnum = config.STEPS_PER_EPOCH

        # warmup is step based, lr is epoch based
        warmup_schedule = [(0, config.BASE_LR / 3), (config.WARMUP * factor, config.BASE_LR)]
        warmup_end_epoch = config.WARMUP * factor * 1. / stepnum
        lr_schedule = [(int(np.ceil(warmup_end_epoch)), warmup_schedule[-1][1])]
        for idx, steps in enumerate(config.LR_SCHEDULE[:-1]):
            mult = 0.1 ** (idx + 1)
            lr_schedule.append(
                (steps * factor // stepnum, config.BASE_LR * mult))
        logger.info("Warm Up Schedule (steps, value): " + str(warmup_schedule))
        logger.info("LR Schedule (epochs, value): " + str(lr_schedule))

        callbacks = [
            PeriodicCallback(
                ModelSaver(max_to_keep=10, keep_checkpoint_every_n_hours=1),
                every_k_epochs=20),
            # linear warmup
            ScheduledHyperParamSetter(
                'learning_rate', warmup_schedule, interp='linear', step_based=True),
            ScheduledHyperParamSetter('learning_rate', lr_schedule),
            EvalCallback(),
            PeakMemoryTracker(),
            EstimatedTimeLeft(),
            SessionRunTimeout(60000).set_chief_only(True),   # 1 minute timeout
        ]
        if not is_horovod:
            callbacks.append(GPUUtilizationTracker())

        cfg = TrainConfig(
            model=get_model(),
            data=QueueInput(get_train_dataflow()),
            callbacks=callbacks,
            steps_per_epoch=stepnum,
            max_epoch=config.LR_SCHEDULE[-1] * factor // stepnum,
            session_init=get_model_loader(args.load) if args.load else None,
        )
        if is_horovod:
            # horovod mode has the best speed for this model
            trainer = HorovodTrainer()
        else:
            # nccl mode has better speed than cpu mode
            trainer = SyncMultiGPUTrainerReplicated(config.NUM_GPUS, mode='nccl')
        launch_train_with_config(cfg, trainer)
