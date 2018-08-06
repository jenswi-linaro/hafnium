#include "vm.h"

#include "api.h"
#include "cpu.h"
#include "std.h"

bool vm_init(struct vm *vm, uint32_t id, uint32_t vcpu_count)
{
	uint32_t i;

	memset(vm, 0, sizeof(*vm));

	vm->vcpu_count = vcpu_count;
	vm->rpc.state = rpc_state_idle;

	/* Do basic initialization of vcpus. */
	for (i = 0; i < vcpu_count; i++) {
		vcpu_init(vm->vcpus + i, vm);
	}

	return mm_ptable_init(&vm->ptable, id, 0);
}

/* TODO: Shall we use index or id here? */
void vm_start_vcpu(struct vm *vm, size_t index, ipaddr_t entry, size_t arg,
		   bool is_primary)
{
	struct vcpu *vcpu = vm->vcpus + index;
	if (index < vm->vcpu_count) {
		arch_regs_init(&vcpu->regs, entry, arg, is_primary);
		vcpu_on(vcpu);
	}
}

void vm_set_current(struct vm *vm)
{
	arch_cpu_update(vm == &primary_vm);
	arch_mm_set_vm(vm->ptable.id, vm->ptable.table);
}
