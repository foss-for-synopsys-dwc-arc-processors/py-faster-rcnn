all:
	python setup.py build_ext --inplace
	rm -rf build

clean:
	rm -f utils/bbox.c utils/cython_bbox.so utils/*.pyc pycocotools/*.pyc pycocotools/_mask.c pycocotools/_mask.so nms/*.so nms/cpu_nms.c nms/gpu_nms.cpp
