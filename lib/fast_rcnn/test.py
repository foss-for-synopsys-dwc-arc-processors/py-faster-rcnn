# --------------------------------------------------------
# Fast R-CNN
# Copyright (c) 2015 Microsoft
# Licensed under The MIT License [see LICENSE for details]
# Written by Ross Girshick
# --------------------------------------------------------

"""Test a Fast R-CNN network on an imdb (image database)."""

from fast_rcnn.config import cfg, get_output_dir
from fast_rcnn.bbox_transform import clip_boxes, bbox_transform_inv
import argparse
from utils.timer import Timer
import numpy as np
import cv2
import caffe
from fast_rcnn.nms_wrapper import nms
import cPickle
from utils.blob import im_list_to_blob
import os
import sys
import operator

def _get_image_blob(im):
    """Converts an image into a network input.

    Arguments:
        im (ndarray): a color image in BGR order

    Returns:
        blob (ndarray): a data blob holding an image pyramid
        im_scale_factors (list): list of image scales (relative to im) used
            in the image pyramid
    """
    im_orig = im.astype(np.float32, copy=True)
    im_orig -= cfg.PIXEL_MEANS

    im_shape = im_orig.shape
    im_size_min = np.min(im_shape[0:2])
    im_size_max = np.max(im_shape[0:2])

    processed_ims = []
    im_scale_factors = []
    if 1:
        desired_height = 600
        desired_width = 1000

        #im = np.zeros((desired_height, desired_width, 3), np.float)
        im_scale = min(desired_height/float(im_shape[0]), desired_width/float(im_shape[1]))
        im_part = cv2.resize(im_orig, None, None, fx=im_scale, fy=im_scale, interpolation=cv2.INTER_LINEAR)
        im = cv2.copyMakeBorder(im_part, 0, desired_height-im_part.shape[0], 0, desired_width - im_part.shape[1], cv2.BORDER_CONSTANT, value=[0,0,0])
        im_scale_factors.append(im_scale)
        processed_ims.append(im)
    else:
        for target_size in cfg.TEST.SCALES:
            im_scale = float(target_size) / float(im_size_min)
            # Prevent the biggest axis from being more than MAX_SIZE
            if np.round(im_scale * im_size_max) > cfg.TEST.MAX_SIZE:
                im_scale = float(cfg.TEST.MAX_SIZE) / float(im_size_max)
            im = cv2.resize(im_orig, None, None, fx=im_scale, fy=im_scale,
                            interpolation=cv2.INTER_LINEAR)
            im_scale_factors.append(im_scale)
            processed_ims.append(im)

    # Create a blob to hold the input images
    blob = im_list_to_blob(processed_ims)

    return blob, np.array(im_scale_factors)

def _get_rois_blob(im_rois, im_scale_factors):
    """Converts RoIs into network inputs.

    Arguments:
        im_rois (ndarray): R x 4 matrix of RoIs in original image coordinates
        im_scale_factors (list): scale factors as returned by _get_image_blob

    Returns:
        blob (ndarray): R x 5 matrix of RoIs in the image pyramid
    """
    rois, levels = _project_im_rois(im_rois, im_scale_factors)
    rois_blob = np.hstack((levels, rois))
    return rois_blob.astype(np.float32, copy=False)

def _project_im_rois(im_rois, scales):
    """Project image RoIs into the image pyramid built by _get_image_blob.

    Arguments:
        im_rois (ndarray): R x 4 matrix of RoIs in original image coordinates
        scales (list): scale factors as returned by _get_image_blob

    Returns:
        rois (ndarray): R x 4 matrix of projected RoI coordinates
        levels (list): image pyramid levels used by each projected RoI
    """
    im_rois = im_rois.astype(np.float, copy=False)

    if len(scales) > 1:
        widths = im_rois[:, 2] - im_rois[:, 0] + 1
        heights = im_rois[:, 3] - im_rois[:, 1] + 1

        areas = widths * heights
        scaled_areas = areas[:, np.newaxis] * (scales[np.newaxis, :] ** 2)
        diff_areas = np.abs(scaled_areas - 224 * 224)
        levels = diff_areas.argmin(axis=1)[:, np.newaxis]
    else:
        levels = np.zeros((im_rois.shape[0], 1), dtype=np.int)

    rois = im_rois * scales[levels]

    return rois, levels

def _get_blobs(im, rois):
    """Convert an image and RoIs within that image into network inputs."""
    blobs = {'data' : None, 'rois' : None}
    blobs['data'], im_scale_factors = _get_image_blob(im)
    if not cfg.TEST.HAS_RPN:
        blobs['rois'] = _get_rois_blob(rois, im_scale_factors)
    return blobs, im_scale_factors


class evdumpparser:
    class evmapdata:
        def __init__(self, array):
            self.layout_type = array[0]
            self.element_type = array[1]
            self.element_size = array[2]
            self.num_maps = array[3]
            self.width = array[4]
            self.height = array[5]

            self.scale = np.frombuffer(array[8:12], dtype=np.float32)[0]

            self.bbox = np.frombuffer(array[16:32], dtype=np.int32)
            self.bbox_alloc_count = self.bbox[0]
            self.bbox_valid_count = self.bbox[1]
            self.bbox_bbox_scale = self.bbox[2]
            self.bbox_confidence_scale = self.bbox[3]

            self.layer_shape = [1, self.num_maps, self.height, self.width]
            if self.layout_type == 2:
                self.layer_shape = [1, 1, self.bbox_alloc_count, 7]
            pass

    def __init__(self, dumpdir, layer_names=None):
        self.dumpdir = dumpdir
        self.layermap = {}
        if layer_names is not None:
            for layer_name in layer_names:
                layermap = self.parse_mapdata(layer_name)
                if layermap is not None:
                    self.layermap[layer_name] = layermap
        pass

    def parse_mapdata(self, layer_name):
        mapdata_file = os.path.join(self.dumpdir, "{}-mapdata.bin".format(layer_name))
        if os.path.exists(mapdata_file) == False:
            return None
        mapdata_arr = np.fromfile(mapdata_file, dtype=np.int8)
        if mapdata_arr.size < 48:
            return None
        return self.evmapdata(mapdata_arr)

    def parse_layerdump(self, layer_name, idx, layer_shape=None):
        layerdump_file = os.path.join(self.dumpdir, '%s-%08d.bin'%(layer_name, idx))
        if os.path.exists(layerdump_file) == False:
            return None
        if layer_name not in self.layermap:
            layermap = self.parse_mapdata(layer_name)
            if layermap is not None:
                self.layermap[layer_name] = layermap
            else:
                return None

        layermap = self.layermap[layer_name]

        new_shape = layermap.layer_shape
        if layermap.layout_type == 2: # CNN_LAYOUT_BBOX
            dumpfixed = np.fromfile(layerdumpfile, dtype=np.float)
            bbox_cnt = dumpfixed.size / self.layer_shape[3]
            new_shape = [1, 1, bbox_cnt, self.layer_shape[3]]
        else: # CNN_LAYOUT_3DCONTIGUOUS or CNN_LAYOUT_3DFMAPS
            if layermap.element_size == 1: # 8bit
                dumpfixed = np.fromfile(layerdump_file, dtype=np.int8)
            else:
                dumpfixed = np.fromfile(layerdump_file, dtype=np.int16)
            dumpfixed = dumpfixed / layermap.scale
            new_shape[0] = dumpfixed.size / (new_shape[1] * new_shape[2] * new_shape[3])
        if layer_shape is not None:
            new_shape = []
            new_size = 1
            for shape in layer_shape:
                new_shape.append(shape)
                new_size = new_size * shape
            new_shape[0] = dumpfixed.size / (new_size / new_shape[0])
        # Reshape the data parsed
        dumpfixed = np.reshape(dumpfixed, new_shape)
        return dumpfixed

def im_detect(net, im, boxes=None, img_idx=0):
    """Detect object classes in an image given object proposals.

    Arguments:
        net (caffe.Net): Fast R-CNN network to use
        im (ndarray): color image to test (in BGR order)
        boxes (ndarray): R x 4 array of object proposals or None (for RPN)

    Returns:
        scores (ndarray): R x K array of object class scores (K includes
            background as object category 0)
        boxes (ndarray): R x (4*K) array of predicted bounding boxes
    """
    blobs, im_scales = _get_blobs(im, boxes)

    # When mapping from image ROIs to feature map ROIs, there's some aliasing
    # (some distinct image ROIs get mapped to the same feature ROI).
    # Here, we identify duplicate feature ROIs, so we only compute features
    # on the unique subset.
    if cfg.DEDUP_BOXES > 0 and not cfg.TEST.HAS_RPN:
        v = np.array([1, 1e3, 1e6, 1e9, 1e12])
        hashes = np.round(blobs['rois'] * cfg.DEDUP_BOXES).dot(v)
        _, index, inv_index = np.unique(hashes, return_index=True,
                                        return_inverse=True)
        blobs['rois'] = blobs['rois'][index, :]
        boxes = boxes[index, :]

    if cfg.TEST.HAS_RPN:
        im_blob = blobs['data']
        blobs['im_info'] = np.array(
            [[im_blob.shape[2], im_blob.shape[3], im_scales[0]]],
            dtype=np.float32)

    # reshape network inputs
    net.blobs['data'].reshape(*(blobs['data'].shape))
    if cfg.TEST.HAS_RPN:
        net.blobs['im_info'].reshape(*(blobs['im_info'].shape))
    else:
        net.blobs['rois'].reshape(*(blobs['rois'].shape))

    # do forward
    forward_kwargs = {'data': blobs['data'].astype(np.float32, copy=False)}
    if cfg.TEST.HAS_RPN:
        forward_kwargs['im_info'] = blobs['im_info'].astype(np.float32, copy=False)
    else:
        forward_kwargs['rois'] = blobs['rois'].astype(np.float32, copy=False)
    blobs_out = net.forward(**forward_kwargs)

    #fixed_dumpdir = "/DATA/hqfang/faster_rcnn_output12bit/"
    fixed_dumpdir = "/DATA/hqfang/faster_rcnn_vgg16_dump/12bit"
    #fixed_dumpdir = "/DATA/hqfang/fixeddump/8bit"
    dumpparser = evdumpparser(fixed_dumpdir)
    #fixed_dumpdir = None
       # score_elem_file = os.path.join(fixed_dumpdir, 'score-elem.bin')
       # bbox_elem_file = os.path.join(fixed_dumpdir, 'bbox-elem.bin')
       # roi_elem_file = os.path.join(fixed_dumpdir, 'roi-elem.bin')
       # if not (os.path.exists(score_elem_file) and os.path.exists(bbox_elem_file) and os.path.exists(roi_elem_file)):
       #     print("{} and {} and {} must exists! Please check!!".format(score_elem_file, bbox_elem_file, roi_elem_file))
       #     sys.exit(1)
    def parse_elem_file(elem_file):
        elems = np.fromfile(elem_file, dtype=np.int8)
        elem_type = elems[0]
        elem_sz = elems[1]
        elem_scale = np.frombuffer(elems[2:], dtype=np.float32)[0]
        return elem_type, elem_sz, elem_scale
    def parse_layer_dump(dumpdir, layer_name, layer_shape, idx):
        elem_file = os.path.join(dumpdir, '%s-elem.bin'%(layer_name))
        out_file  = os.path.join(dumpdir, '%s-%08d.bin'%(layer_name, idx))
        if not (os.path.exists(elem_file) and os.path.exists(out_file)):
            #print("elem file {} or out file {} doesn't exist, please check!".format(elem_file, out_file))
            return None
        elem_type, elem_sz, elem_scale = parse_elem_file(elem_file)
        if elem_sz == 1:
            outfixed = np.fromfile(out_file, dtype=np.int8)
        else:
            outfixed = np.fromfile(out_file, dtype=np.int16)
        outfixed = outfixed / elem_scale
        new_shape = []
        new_size = 1
        for shape in layer_shape:
            new_shape.append(shape)
            new_size = new_size * shape
        new_shape[0] = outfixed.shape[0] / (new_size / new_shape[0])
        if new_shape[0] != layer_shape[0]:
            print("caffe and host-fixed shape not match, {} vs {}".format(layer_shape, new_shape))

        #rpns = outfixed.shape[0] / reduce(operator.mul, layer_shape[1:])
        #new_shape = layer_shape

        #outfixed = outfixed[:reduce(operator.mul, laye)]
        outfixed = np.reshape(outfixed, new_shape)
        return outfixed


    if cfg.TEST.HAS_RPN:
        assert len(im_scales) == 1, "Only single-image batch implemented"
        rois = net.blobs['rois'].data.copy()
        if fixed_dumpdir:
            rois_new = parse_layer_dump(fixed_dumpdir, "roi", rois.shape, img_idx)
            if rois_new is None:
                rois_new = dumpparser.parse_layerdump("output__rois", img_idx, rois.shape)
                if rois_new is None:
                    print("Not able to find rois layer dump binary files")
                    sys.exit(1)
            rois = rois_new
        # unscale back to raw image space
        boxes = rois[:, 1:5] / im_scales[0]

    if cfg.TEST.SVM:
        # use the raw scores before softmax under the assumption they
        # were trained as linear SVMs
        scores = net.blobs['cls_score'].data
    else:
        # use softmax estimated probabilities
        scores = blobs_out['cls_prob']
        if fixed_dumpdir:
            scores_new = parse_layer_dump(fixed_dumpdir, "score", scores.shape, img_idx)
            if scores_new is None:
                scores_new = dumpparser.parse_layerdump("cls_prob", img_idx, scores.shape)
                if scores_new is None:
                    print("Not able to find cls_prob layer dump binary files")
                    sys.exit(1)
            scores = scores_new

    if cfg.TEST.BBOX_REG:
        # Apply bounding-box regression deltas
        box_deltas = blobs_out['bbox_pred']
        if fixed_dumpdir:
            box_deltas_new = parse_layer_dump(fixed_dumpdir, "bbox", box_deltas.shape, img_idx)
            if box_deltas_new is None:
                box_deltas_new = dumpparser.parse_layerdump("bbox_pred", img_idx, box_deltas.shape)
                if box_deltas_new is None:
                    print("Not able to find bbox_pred layer dump binary files")
                    sys.exit(1)
            box_deltas = box_deltas_new
        pred_boxes = bbox_transform_inv(boxes, box_deltas)
        pred_boxes = clip_boxes(pred_boxes, im.shape)
    else:
        # Simply repeat the boxes, once for each class
        pred_boxes = np.tile(boxes, (1, scores.shape[1]))

    if cfg.DEDUP_BOXES > 0 and not cfg.TEST.HAS_RPN:
        # Map scores and predictions back to the original set of boxes
        scores = scores[inv_index, :]
        pred_boxes = pred_boxes[inv_index, :]

    return scores, pred_boxes

def vis_detections(im, class_name, dets, thresh=0.3):
    """Visual debugging of detections."""
    import matplotlib.pyplot as plt
    im = im[:, :, (2, 1, 0)]
    for i in xrange(np.minimum(10, dets.shape[0])):
        bbox = dets[i, :4]
        score = dets[i, -1]
        if score > thresh:
            plt.cla()
            plt.imshow(im)
            plt.gca().add_patch(
                plt.Rectangle((bbox[0], bbox[1]),
                              bbox[2] - bbox[0],
                              bbox[3] - bbox[1], fill=False,
                              edgecolor='g', linewidth=3)
                )
            plt.title('{}  {:.3f}'.format(class_name, score))
            plt.show()

def apply_nms(all_boxes, thresh):
    """Apply non-maximum suppression to all predicted boxes output by the
    test_net method.
    """
    num_classes = len(all_boxes)
    num_images = len(all_boxes[0])
    nms_boxes = [[[] for _ in xrange(num_images)]
                 for _ in xrange(num_classes)]
    for cls_ind in xrange(num_classes):
        for im_ind in xrange(num_images):
            dets = all_boxes[cls_ind][im_ind]
            if dets == []:
                continue
            # CPU NMS is much faster than GPU NMS when the number of boxes
            # is relative small (e.g., < 10k)
            # TODO(rbg): autotune NMS dispatch
            keep = nms(dets, thresh, force_cpu=True)
            if len(keep) == 0:
                continue
            nms_boxes[cls_ind][im_ind] = dets[keep, :].copy()
    return nms_boxes

def test_net(net, imdb, max_per_image=100, thresh=0.05, vis=False):
    """Test a Fast R-CNN network on an image database."""
    num_images = len(imdb.image_index)
    # all detections are collected into:
    #    all_boxes[cls][image] = N x 5 array of detections in
    #    (x1, y1, x2, y2, score)
    all_boxes = [[[] for _ in xrange(num_images)]
                 for _ in xrange(imdb.num_classes)]

    output_dir = get_output_dir(imdb, net)

    # timers
    _t = {'im_detect' : Timer(), 'misc' : Timer()}

    if not cfg.TEST.HAS_RPN:
        roidb = imdb.roidb

    #num_images = 2700
    for i in xrange(num_images):
        # filter out any ground truth boxes
        if cfg.TEST.HAS_RPN:
            box_proposals = None
        else:
            # The roidb may contain ground-truth rois (for example, if the roidb
            # comes from the training or val split). We only want to evaluate
            # detection on the *non*-ground-truth rois. We select those the rois
            # that have the gt_classes field set to 0, which means there's no
            # ground truth.
            box_proposals = roidb[i]['boxes'][roidb[i]['gt_classes'] == 0]

        im = cv2.imread(imdb.image_path_at(i))
        _t['im_detect'].tic()
        scores, boxes = im_detect(net, im, box_proposals, i)
        _t['im_detect'].toc()

        _t['misc'].tic()
        # skip j = 0, because it's the background class
        for j in xrange(1, imdb.num_classes):
            inds = np.where(scores[:, j] > thresh)[0]
            cls_scores = scores[inds, j]
            cls_boxes = boxes[inds, j*4:(j+1)*4]
            cls_dets = np.hstack((cls_boxes, cls_scores[:, np.newaxis])) \
                .astype(np.float32, copy=False)
            keep = nms(cls_dets, cfg.TEST.NMS)
            cls_dets = cls_dets[keep, :]
            if vis:
                vis_detections(im, imdb.classes[j], cls_dets)
            all_boxes[j][i] = cls_dets

        # Limit to max_per_image detections *over all classes*
        if max_per_image > 0:
            image_scores = np.hstack([all_boxes[j][i][:, -1]
                                      for j in xrange(1, imdb.num_classes)])
            if len(image_scores) > max_per_image:
                image_thresh = np.sort(image_scores)[-max_per_image]
                for j in xrange(1, imdb.num_classes):
                    keep = np.where(all_boxes[j][i][:, -1] >= image_thresh)[0]
                    all_boxes[j][i] = all_boxes[j][i][keep, :]
        _t['misc'].toc()

        print 'im_detect: {:d}/{:d} {:.3f}s {:.3f}s' \
              .format(i + 1, num_images, _t['im_detect'].average_time,
                      _t['misc'].average_time)

    det_file = os.path.join(output_dir, 'detections.pkl')
    with open(det_file, 'wb') as f:
        cPickle.dump(all_boxes, f, cPickle.HIGHEST_PROTOCOL)

    print 'Evaluating detections'
    imdb.evaluate_detections(all_boxes, output_dir)
