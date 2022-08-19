#!/bin/bash
#
# Copyright 2020 The Hafnium Authors.
#
# Use of this source code is governed by a BSD-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/BSD-3-Clause.

# TIMEOUT, PROJECT, OUT, LOG_DIR_BASE set in:

USE_FVP=true

while test $# -gt 0
do
  case "$1" in
    --qemu) USE_FVP=false
      ;;
    *) echo "Unexpected argument $1"
      exit 1
      ;;
  esac
  shift
done

KOKORO_DIR="$(dirname "$0")"
source $KOKORO_DIR/test_common.sh

HFTEST=(${TIMEOUT[@]} 300s ./test/hftest/hftest.py)

HFTEST+=(--log "$LOG_DIR_BASE")

if [ $USE_FVP == true ]
then
  SPMC_PATH_VHE="$OUT/secure_aem_v8a_fvp_vhe_clang"
  HYPERVISOR_PATH_VHE="$OUT/aem_v8a_fvp_vhe_clang"
  HFTEST_VHE="${HFTEST[@]} --out_partitions $OUT/secure_aem_v8a_fvp_vhe_vm_clang"
  HFTEST_VHE+=(--spmc "$SPMC_PATH_VHE/hafnium.bin" --driver=fvp)
  HFTEST_VHE+=(--log "$LOG_DIR_BASE/vhe")

  SPMC_PATH="$OUT/secure_aem_v8a_fvp_clang"
  HYPERVISOR_PATH="$OUT/aem_v8a_fvp_clang"
  HFTEST+=(--out_partitions "$OUT/secure_aem_v8a_fvp_vm_clang")
  HFTEST+=(--spmc "$SPMC_PATH/hafnium.bin" --driver=fvp)

  ${HFTEST[@]} --partitions_json test/vmapi/ffa_secure_partition_only/ffa_secure_partition_only_test.json

  ${HFTEST[@]} --hypervisor "$HYPERVISOR_PATH/hafnium.bin" \
               --partitions_json test/vmapi/ffa_secure_partitions/ffa_both_world_partitions_test.json

  ${HFTEST_VHE[@]} --hypervisor "$HYPERVISOR_PATH/hafnium.bin" \
                   --partitions_json test/vmapi/ffa_secure_partitions/ffa_both_world_partitions_vhe_test.json
else
  SPMC_PATH="$OUT/secure_qemu_aarch64_clang"
  HYPERVISOR_PATH="$OUT/qemu_aarch64_clang"
  HFTEST+=(--spmc "$SPMC_PATH/hafnium.bin" --driver=qemu --tfa)

  #TODO: integrate in gn build flow
  cp out/reference/secure_qemu_aarch64_vm_clang/obj/test/vmapi/ffa_secure_partition_only/qemu_ffa_secure_partition_only_test_package/qemu_ffa_secure_partition_only_test_package.img out/reference/secure_qemu_aarch64_clang/bl32_extra1.bin

  #TODO: integrate in gn build flow
  dtc -I dts -O dtb -o out/reference/secure_qemu_aarch64_clang/fvp_tsp_fw_config.dtb test/vmapi/qemu-spmc.dts

  #TODO: re-use --partitions_json option
  ${HFTEST[@]}
fi
