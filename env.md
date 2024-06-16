# Environment Setup for RegionPVT
## Create a new conda environment

```bash
conda create -n RegionPVT python=3.8
```

## Install pytorch-1.8.1

```bash
# pytorch-1.7.1 works, too
conda install pytorch==1.8.1 torchvision==0.9.1 torchaudio==0.8.1 cudatoolkit=10.2 -c pytorch
```

## Install other dependences

Fix dependence, caused by install torch-points3d, the version of `numpy` `SharedArray` must be `1.19.5` `3.2.0` for torch-1.8.1, `1.19.5` `3.2.1` for torch-1.7.1

```bash
pip install h5py==3.2.1 matplotlib==3.4.2 numpy==1.19.5 Pillow==9.1.0 PyYAML==6.0 scipy==1.6.3 setuptools==50.3.1 SharedArray==3.2.0 tensorboardX==2.5 termcolor==1.1.0 timm==0.4.9
```

## Install torch_geometric

```bash
pip install torch_scatter==2.0.8 torch_sparse==0.6.12 torch_cluster==1.5.9
pip install torch_geometric==1.7.2
```

Just install the newest version of `torch_scatter` `torch_sparse` `torch_cluster` at https://data.pyg.org/whl/torch-1.8.1%2Bcu102.html

## Install torch_points3d

```bash
# torch_geometric-1.7.0 for torch-1.7.1, torch_geometric-1.7.2 for torch-1.8.1
pip install torch_points3d==1.3.0
```

## Compile pointops2

Make sure you have installed `gcc` and `cuda`, and `nvcc` can work (Note that if you install cuda by conda, it won't provide nvcc and you should install cuda manually.). Then, compile and install pointops2 as follows. (We have tested on `gcc==7.5.0` and `cuda==10.2`). `Next step is try compile pointops2 based on torch-1.12.0`

```bash
cd lib/pointops2
python3 setup.py install
cd ../..
```

## Compile MinkowskiEngine

```bash
# For CUDA 10.2, must use GCC < 8, our GCC is 7.5.0 
sudo apt-get install libopenblas-dev
cd thirdparty
git clone https://github.com/NVIDIA/MinkowskiEngine.git
cd thirdparty/MinkowskiEngine-master
python setup.py install --blas_include_dirs=${CONDA_PREFIX}/include --blas=openblas
cd ../..
```

## Other dependences for FastPointTransformer

```bash
# For cuda_ops
pip install gin-config
```

## Compile cuda_ops for LightWeightSelfAttention

```bash
cd src/cuda_ops
pip3 install .
cd ../..
```

