# Regional-to-Local Point-Voxel Transformer (RegionPVT)

This is the official PyTorch implementation of the paper **Regional-to-Local Point-Voxel Transformer for Large-scale Indoor 3D Point Cloud Semantic Segmentation**

<div style="text-align:center">
<img src="./figs/R2L_Encoder_Block.png">
</div>

# Get Started

## Environment

1. Install dependencies

```bash
pip install -r requirements.txt
```

2. Compile pointops2

Make sure you have installed `gcc` and `cuda`, and `nvcc` can work (Note that if you install cuda by conda, it won't provide nvcc and you should install cuda manually.). Then, compile and install pointops2 as follows. (We have tested on `gcc==7.5.0\9.4.0`, `pytorch==1.12.1` and `cuda==11.3`).

```bash
# pytorch > 1.12.1 is also OK
cd lib/pointops2
python3 setup.py install
cd ../..
```

3. Compile MinkowskiEngine

```bash
# For CUDA 10.2, must use GCC < 8, our GCC is 9.4.0
sudo apt-get install libopenblas-dev
cd thirdparty
git clone https://github.com/NVIDIA/MinkowskiEngine.git
cd MinkowskiEngine-master
python setup.py install --blas_include_dirs=${CONDA_PREFIX}/include --blas=openblas
cd ../..
```
4. Compile cuda_ops for LightWeightSelfAttention

```bash
cd libs/cuda_ops
pip3 install .
cd ../..
```

See [env.md](env.md) for more details.

## Datasets Preparation

### S3DIS
Please refer to https://github.com/yanx27/Pointnet_Pointnet2_pytorch for S3DIS preprocessing. Then modify the `data_root` entry in the .yaml configuration file.

### ScanNetv2
Please refer to https://github.com/dvlab-research/PointGroup for the ScanNetv2 preprocessing. Then change the `data_root` entry in the .yaml configuration file accordingly.

See [data_utils](data_utils/README.md) for more details.

## Training

### S3DIS

```bash
python3 train_regionpvt.py --config config/s3dis/s3dis_regionpvt.yaml
```

### ScanNetv2

```bash
python3 train_regionpvt.py --config config/scannetv2/scannetv2_regionpvt.yaml
```

Note: It is normal to see the the results on S3DIS fluctuate between -0.5\% and +0.5\% mIoU maybe because the size of S3DIS is relatively small, while the results on ScanNetv2 are relatively stable.


## Testing
For testing, first change the `model_path`, `save_folder` and `data_root_val` (if applicable) accordingly. Then, run the following command. 

```bash
python3 test_regionpvt.py --config [YOUR_CONFIG_PATH]
```

## Acknowledgements

Our code is based on the [Stratified-Transformer](https://github.com/dvlab-research/Stratified-Transformer) and [FastPointTransformer](https://github.com/POSTECH-CVLab/FastPointTransformer). If you use our model, please consider citing them as well.