/*
 * Copyright 2021 The Hafnium Authors.
 *
 * Use of this source code is governed by a BSD-style
 * license that can be found in the LICENSE file or at
 * https://opensource.org/licenses/BSD-3-Clause.
 */

#ifndef FFA_MEMORY_HANDLE_H
#define FFA_MEMORY_HANDLE_H

#include "hf/ffa.h"

/**
 * Encodes memory handle according to section 5.10.2 of the FF-A v1.0 spec.
 */
ffa_memory_handle_t ffa_memory_handle_make(uint64_t index);

/**
 * Checks whether given handle was allocated by current world, according to
 * handle encoding rules.
 */
bool ffa_memory_handle_allocated_by_current_world(ffa_memory_handle_t handle);

#endif
