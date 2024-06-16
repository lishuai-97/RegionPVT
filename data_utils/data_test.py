import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
sys.path.append(BASE_DIR)

print(f'BASE_DIR: {BASE_DIR}')
print(f'ROOT_DIR: {ROOT_DIR}')