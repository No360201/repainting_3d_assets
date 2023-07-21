#!/usr/bin/env bash
set -e
set -x

USE_CODE_FROM_DOCKER=0

if [ $# -lt 3 ]; then
    echo "Error: Insufficient command line arguments:"
    echo "$0 <path_dataset_shapenet> <path_outputs> [list_of_shapenet_model_ids]"
    exit 255
fi

SL3A_DATASET_ROOT="${1}"
shift

if [ ! -f "${SL3A_DATASET_ROOT}/.marker.dataset.shapenet.completed" ]; then
    echo "Invalid dataset path: ${SL3A_DATASET_ROOT}. Use script/setup_dataset_shapenet.sh to download it first."
    exit 255
fi

SL3A_OUT_SHAPENET="${1}"
shift

mkdir -p "${SL3A_OUT_SHAPENET}"

SELF=$(realpath "$0")
SELF_DIR=$(dirname "${SELF}")
SL3A_CODE_ROOT=$(realpath "${SELF_DIR}/..")
CMD_MOUNT_LATEST_CODE=""
if [ ! "${USE_CODE_FROM_DOCKER}" -eq "1" ]; then
    CMD_MOUNT_LATEST_CODE="-v "${SL3A_CODE_ROOT}":/sl3a/code"
fi

nvidia-docker run \
    -it \
    --rm \
    -v "${SL3A_DATASET_ROOT}":/sl3a/dataset \
    -v "${SL3A_OUT_SHAPENET}":/sl3a/out_shapenet \
    ${CMD_MOUNT_LATEST_CODE} \
    --user $(id -u):$(id -g) \
    --ulimit core=0:0 \
    second_life_3d_assets \
    bash /sl3a/code/scripts/conda_run_shapenet.sh /sl3a "$@"