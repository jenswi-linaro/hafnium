/*
 * Copyright 2020 The Hafnium Authors.
 *
 * Use of this source code is governed by a BSD-style
 * license that can be found in the LICENSE file or at
 * https://opensource.org/licenses/BSD-3-Clause.
 */

#define SPMD_ID 0xFFFF
#define SPMD_DIRECT_MSG_SET_ENTRY_POINT 1

/**
 * Get SPMC ID stored in the manifest.
 */
ffa_vm_id_t get_ffa_id(void);
