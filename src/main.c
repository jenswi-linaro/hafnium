/*
 * Copyright 2018 The Hafnium Authors.
 *
 * Use of this source code is governed by a BSD-style
 * license that can be found in the LICENSE file or at
 * https://opensource.org/licenses/BSD-3-Clause.
 */

#include "hf/cpu.h"
#include "hf/dlog.h"
#include "hf/vm.h"

/**
 * The entry point of CPUs when they are turned on. It is supposed to initialise
 * all state and return the first vCPU to run.
 */
struct vcpu *cpu_main(struct cpu *c)
{
	struct vm *first_boot;
	struct vcpu *vcpu;

#if SECURE_WORLD == 1
	if (c->is_on == false) {
		/*
		 * This is the PSCI warm reset path (svc_cpu_on_finish
		 * handler relayed by SPMD). Notice currenty the "first_boot"
		 * VM is resumed on any CPU on event.
		 */
		(void)cpu_on(c, ipa_init(0UL), 0UL);
		dlog_verbose("%s: cpu mpidr 0x%x on\n", __func__, c->id);
	}
#endif

	/*
	 * This returns the PVM in the normal worls and the first
	 * booted Secure Partition in the secure world.
	 */
	first_boot = vm_get_first_boot();

	vcpu = vm_get_vcpu(first_boot, cpu_index(c));

	if (vcpu->psci_handler[0].ipa) {
		struct vcpu_locked vcpu_locked = vcpu_lock(vcpu);

		vcpu_on(vcpu_locked, vcpu->psci_handler[0], 0);
		vcpu_unlock(&vcpu_locked);
	}

	vcpu->cpu = c;

	arch_cpu_init();

	/* Reset the registers to give a clean start for vCPU. */
	arch_regs_reset(vcpu);

	return vcpu;
}
