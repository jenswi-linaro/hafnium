/*
 * Copyright 2018 The Hafnium Authors.
 *
 * Use of this source code is governed by a BSD-style
 * license that can be found in the LICENSE file or at
 * https://opensource.org/licenses/BSD-3-Clause.
 */

#include "hf/arch/offsets.h"
#include "msr.h"

/**
 * Called only on first boot after the image has been relocated and BSS zeroed.
 *
 * It is required that caches be clean and invalid.
 */
.section .init.image_entry, "ax"
.global image_entry
image_entry:
	/* Interpret the registers passed from the loader. */
	bl plat_boot_flow_hook

	/* Get pointer to first CPU. */
	adrp x28, cpus
	add x28, x28, :lo12:cpus

	/* Set the ID of this CPU from the affinity bits of mpidr. */
	mrs x30, mpidr_el1
	ubfx x29, x30, 0, 24
	ubfx x30, x30, 32, 8
	orr x30, x29, x30, lsl #32
	str x30, [x28, CPU_ID]

	mov x0, x28
	bl prepare_for_c

	/*
	 * Call into C to initialize the memory management configuration with
	 * MMU and caches disabled. Result will be stored in `arch_mm_config`.
	 */
	bl one_time_init_mm

	/* Enable MMU and caches before running the rest of initialization. */
	bl mm_enable
	bl one_time_init

	/* Begin steady state operation. */
	mov x0, x28
	b cpu_init

/**
 * Entry point for all cases other than the first boot e.g. secondary CPUs and
 * resuming from suspend.
 *
 * It is required that caches be coherent but not necessarily clean or invalid.
 *
 * x0 points to the current CPU.
 */
.section .text.entry, "ax"
.global cpu_entry
cpu_entry:
	bl mm_enable
	bl prepare_for_c

	/* Intentional fallthrough. */

cpu_init:
	/* Call into C code, x0 holds the CPU pointer. */
	bl cpu_main

	/* Run the vCPU returned by cpu_main. */
	bl vcpu_restore_all_and_run

	/* Loop forever waiting for interrupts. */
0:	wfi
	b 0b

/**
 * Set up CPU environment for executing C code. This is called on first boot
 * with caches disabled but subsequent calls will have caches enabled.
 *
 * x0 points to the current CPU on entry and exit.
 */
prepare_for_c:
	/* Use SPx (instead of SP0). */
	msr spsel, #1

	/* Prepare the stack. */
	ldr x1, [x0, #CPU_STACK_BOTTOM]
	mov sp, x1

	/* Configure exception handlers. */
	adr x2, vector_table_el2
	msr vbar_el2, x2
	ret

/**
 * Applies the memory management configuration to the CPU, preserving x0 along
 * the way.
 */
mm_enable:
	/*
	 * Invalidate any potentially stale local TLB entries for the
	 * hypervisor's stage-1 and the VM's stage-2 before they start being
	 * used. The VM's stage-1 is invalidated as a side effect but it wasn't
	 * using it yet anyway.
	 */
	tlbi alle2
	tlbi vmalls12e1

	/*
	 * Load and apply the memory management configuration. Order depends on
	 * `struct arch_mm_config.
	 */
	adrp x6, arch_mm_config
	add x6, x6, :lo12:arch_mm_config

	ldp x1, x2, [x6]
	ldp x3, x4, [x6, #16]
	ldp x5, x6, [x6, #32]

	msr ttbr0_el2, x1
	msr vtcr_el2, x2

#if SECURE_WORLD == 1
	msr MSR_VSTCR_EL2, x6
#endif

	msr mair_el2, x3
	msr tcr_el2, x4

	/* Ensure everything before this point has completed. */
	dsb sy
	isb

	/*
	 * Configure sctlr_el2 to enable MMU and cache and don't proceed until
	 * this has completed.
	 */
	msr sctlr_el2, x5
	isb
	ret
