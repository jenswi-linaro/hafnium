#include <stdalign.h>
#include <stddef.h>

#include "alloc.h"
#include "api.h"
#include "cpio.h"
#include "cpu.h"
#include "dlog.h"
#include "fdt.h"
#include "mm.h"
#include "std.h"
#include "vm.h"

void *fdt;
char ptable_buf[PAGE_SIZE * 20];
struct mm_ptable ptable;

bool fdt_find_node(struct fdt_node *node, const char *path)
{
	while (*path) {
		if (!fdt_find_child(node, path)) {
			return false;
		}
		path += strlen(path);
	}

	return true;
}

static uint64_t convert_number(const char *data, uint32_t size)
{
	union {
		volatile uint64_t v;
		char a[8];
	} t;

	switch (size) {
	case sizeof(uint32_t):
		return be32toh(*(uint32_t *)data);
	case sizeof(uint64_t):
		memcpy(t.a, data, sizeof(uint64_t));
		return be64toh(t.v);
	default:
		return 0;
	}
}

static bool fdt_read_number(const struct fdt_node *node, const char *name,
			    uint64_t *value)
{
	const char *data;
	uint32_t size;

	if (!fdt_read_property(node, name, &data, &size)) {
		return false;
	}

	switch (size) {
	case sizeof(uint32_t):
	case sizeof(uint64_t):
		*value = convert_number(data, size);
		break;

	default:
		return false;
	}

	return true;
}

bool fdt_write_number(struct fdt_node *node, const char *name, uint64_t value)
{
	const char *data;
	uint32_t size;
	union {
		volatile uint64_t v;
		char a[8];
	} t;

	if (!fdt_read_property(node, name, &data, &size)) {
		return false;
	}

	switch (size) {
	case sizeof(uint32_t):
		*(uint32_t *)data = be32toh(value);
		break;

	case sizeof(uint64_t):
		t.v = be64toh(value);
		memcpy((void *)data, t.a, sizeof(uint64_t));
		break;

	default:
		return false;
	}

	return true;
}

/**
 * Copies data to an unmapped location by mapping it for write, copying the
 * data, then unmapping it.
 */
static bool copy_to_unmaped(paddr_t to, const void *from, size_t size)
{
	if (!mm_ptable_map(&ptable, (vaddr_t)to, (vaddr_t)to + size, to,
			   MM_MODE_W | MM_MODE_STAGE1)) {
		return false;
	}

	memcpy((void *)to, from, size);

	mm_ptable_unmap(&ptable, to, to + size, MM_MODE_STAGE1);

	return true;
}

static bool relocate(const char *from, size_t size)
{
	/* TODO: This is a hack. We must read the alignment from the binary. */
	extern char bin_end[];
	size_t tmp = (size_t)&bin_end[0];
	paddr_t dest = (tmp + 0x80000 - 1) & ~(0x80000 - 1);
	dlog("bin_end is at %p, copying to %p\n", &bin_end[0], dest);
	return copy_to_unmaped(dest, from, size);
}

static void find_memory_range(const struct fdt_node *root,
			      uint64_t *block_start, uint64_t *block_size)
{
	struct fdt_node n = *root;
	const char *name;
	uint64_t address_size;
	uint64_t size_size;
	uint64_t entry_size;

	/* Get the sizes of memory range addresses and sizes. */
	if (fdt_read_number(&n, "#address-cells", &address_size)) {
		address_size *= sizeof(uint32_t);
	} else {
		address_size = sizeof(uint32_t);
	}

	if (fdt_read_number(&n, "#size-cells", &size_size)) {
		size_size *= sizeof(uint32_t);
	} else {
		size_size = sizeof(uint32_t);
	}

	entry_size = address_size + size_size;

	/* Look for nodes with the device_type set to "memory". */
	if (!fdt_first_child(&n, &name)) {
		return;
	}

	do {
		const char *data;
		uint32_t size;
		if (!fdt_read_property(&n, "device_type", &data, &size) ||
		    size != sizeof("memory") ||
		    memcmp(data, "memory", sizeof("memory")) != 0 ||
		    !fdt_read_property(&n, "reg", &data, &size)) {
			continue;
		}

		/* Traverse all memory ranges within this node. */
		while (size >= entry_size) {
			uint64_t addr = convert_number(data, address_size);
			uint64_t len =
				convert_number(data + address_size, size_size);

			if (len > *block_size) {
				/* Remember the largest range we've found. */
				*block_start = addr;
				*block_size = len;
			}

			size -= entry_size;
			data += entry_size;
		}
	} while (fdt_next_sibling(&n, &name));

	/* TODO: Check for "reserved-memory" nodes. */
}

/**
 * Finds the memory region where initrd is stored, and udpates the fdt node
 * cursor to the node called "chosen".
 */
static bool find_initrd(struct fdt_node *n, uint64_t *begin, uint64_t *end)
{
	if (!fdt_find_node(n, "chosen\0")) {
		dlog("Unable to find 'chosen'\n");
		return false;
	}

	if (!fdt_read_number(n, "linux,initrd-start", begin)) {
		dlog("Unable to read linux,initrd-start\n");
		return false;
	}

	if (!fdt_read_number(n, "linux,initrd-end", end)) {
		dlog("Unable to read linux,initrd-end\n");
		return false;
	}

	return true;
}

struct memiter {
	const char *next;
	const char *limit;
};

static void memiter_init(struct memiter *it, const void *data, size_t size)
{
	it->next = data;
	it->limit = it->next + size;
}

static bool memiter_isspace(struct memiter *it)
{
	switch (*it->next) {
	case ' ':
	case '\t':
	case '\n':
	case '\r':
		return true;
	default:
		return false;
	}
}

static void memiter_skip_space(struct memiter *it)
{
	while (it->next < it->limit && memiter_isspace(it)) {
		it->next++;
	}
}

static bool memiter_iseq(const struct memiter *it, const char *str)
{
	size_t len = strlen(str);
	if (len != it->limit - it->next) {
		return false;
	}
	return memcmp(it->next, str, len) == 0;
}

static bool memiter_parse_str(struct memiter *it, struct memiter *str)
{
	/* Skip all white space and fail if we reach the end of the buffer. */
	memiter_skip_space(it);
	if (it->next >= it->limit) {
		return false;
	}

	str->next = it->next;

	/* Find the end of the string. */
	while (it->next < it->limit && !memiter_isspace(it)) {
		it->next++;
	}

	str->limit = it->next;

	return true;
}

static bool memiter_parse_uint(struct memiter *it, uint64_t *value)
{
	uint64_t v = 0;

	/* Skip all white space and fail if we reach the end of the buffer. */
	memiter_skip_space(it);
	if (it->next >= it->limit) {
		return false;
	}

	/* Fail if it's not a number. */
	if (*it->next < '0' || *it->next > '9') {
		return false;
	}

	/* Parse the number. */
	do {
		v = v * 10 + *it->next - '0';
		it->next++;
	} while (it->next < it->limit && *it->next >= '0' && *it->next <= '9');

	*value = v;

	return true;
}

static bool memiter_find_file(struct cpio *c, const struct memiter *filename,
			      struct memiter *it)
{
	const char *fname;
	const void *fcontents;
	size_t fsize;
	struct cpio_iter iter;

	cpio_init_iter(c, &iter);

	while (cpio_next(&iter, &fname, &fcontents, &fsize)) {
		if (memiter_iseq(filename, fname)) {
			memiter_init(it, fcontents, fsize);
			return true;
		}
	}

	return false;
}

static bool find_file(struct cpio *c, const char *name, struct memiter *it)
{
	const char *fname;
	const void *fcontents;
	size_t fsize;
	struct cpio_iter iter;

	cpio_init_iter(c, &iter);

	while (cpio_next(&iter, &fname, &fcontents, &fsize)) {
		if (!strcmp(fname, name)) {
			memiter_init(it, fcontents, fsize);
			return true;
		}
	}

	return false;
}

static bool load_secondary(struct cpio *c, uint64_t mem_start,
			   uint64_t *mem_size)
{
	struct memiter it;
	struct memiter str;
	uint64_t mem;
	uint64_t cpu;
	uint32_t count;

	if (!find_file(c, "vms.txt", &it)) {
		dlog("Unable to find vms.txt\n");
		return false;
	}

	for (count = 0;
	     memiter_parse_uint(&it, &mem) && memiter_parse_uint(&it, &cpu) &&
	     memiter_parse_str(&it, &str) && count < MAX_VMS;
	     count++) {
		struct memiter kernel;

		if (!memiter_find_file(c, &str, &kernel)) {
			dlog("Unable to load kernel for vm %u\n", count);
			continue;
		}

		if (mem > *mem_size) {
			dlog("Not enough memory for vm %u (%u bytes)\n", count,
			     mem);
			continue;
		}

		if (mem < kernel.limit - kernel.next) {
			dlog("Kernel is larger than available memory for vm "
			     "%u\n",
			     count);
			continue;
		}

		*mem_size -= mem;
		if (!copy_to_unmaped(mem_start + *mem_size, kernel.next,
				     kernel.limit - kernel.next)) {
			dlog("Unable to copy kernel for vm %u\n", count);
			continue;
		}

		dlog("Loaded VM%u with %u vcpus, entry at 0x%x\n", count, cpu,
		     mem_start + *mem_size);
		vm_init(secondary_vm + count, cpu);
		vm_start_vcpu(secondary_vm + count, 0, mem_start + *mem_size, 0,
			      false);
	}

	secondary_vm_count = count;

	return true;
}

static bool load_primary(struct cpio *c, struct fdt_node *chosen)
{
	struct memiter it;

	if (!find_file(c, "vmlinuz", &it)) {
		dlog("Unable to find vmlinuz\n");
		return false;
	}

	if (!relocate(it.next, it.limit - it.next)) {
		dlog("Unable to relocate kernel for primary vm.\n");
		return false;
	}

	if (!find_file(c, "initrd.img", &it)) {
		dlog("Unable to find initrd.img\n");
		return false;
	}

	/* Patch FDT to point to new ramdisk. */
	if (!fdt_write_number(chosen, "linux,initrd-start", (size_t)it.next)) {
		dlog("Unable to write linux,initrd-start\n");
		return false;
	}

	if (!fdt_write_number(chosen, "linux,initrd-end", (size_t)it.limit)) {
		dlog("Unable to write linux,initrd-end\n");
		return false;
	}

	/*
	 * Patch fdt to reserve memory.
	 */
	{
		size_t tmp = (size_t)&relocate;
		tmp = (tmp + 0x80000 - 1) & ~(0x80000 - 1);
		fdt_add_mem_reservation(fdt, tmp & ~0xfffff, 0x80000);
		vm_init(&primary_vm, MAX_CPUS);
		vm_start_vcpu(&primary_vm, 0, tmp, (size_t)fdt, true);
	}

	return true;
}

/**
 * Performs one-time initialisation of the hypervisor.
 */
static void one_time_init(void)
{
	extern char text_begin[];
	extern char text_end[];
	extern char rodata_begin[];
	extern char rodata_end[];
	extern char data_begin[];
	extern char data_end[];

	dlog("Initializing hafnium\n");

	cpu_module_init();
	halloc_init((size_t)ptable_buf, sizeof(ptable_buf));

	if (!mm_ptable_init(&ptable, MM_MODE_NOSYNC | MM_MODE_STAGE1)) {
		dlog("Unable to allocate memory for page table.\n");
		for (;;) {
			/* do nothing */
		}
	}

	dlog("text: 0x%x - 0x%x\n", text_begin, text_end);
	dlog("rodata: 0x%x - 0x%x\n", rodata_begin, rodata_end);
	dlog("data: 0x%x - 0x%x\n", data_begin, data_end);

	/* Map page for uart. */
	mm_ptable_map_page(&ptable, PL011_BASE, PL011_BASE,
			   MM_MODE_R | MM_MODE_W | MM_MODE_D | MM_MODE_NOSYNC |
				   MM_MODE_STAGE1);

	/* Map each section. */
	mm_ptable_map(&ptable, (vaddr_t)text_begin, (vaddr_t)text_end,
		      (paddr_t)text_begin,
		      MM_MODE_X | MM_MODE_NOSYNC | MM_MODE_STAGE1);

	mm_ptable_map(&ptable, (vaddr_t)rodata_begin, (vaddr_t)rodata_end,
		      (paddr_t)rodata_begin,
		      MM_MODE_R | MM_MODE_NOSYNC | MM_MODE_STAGE1);

	mm_ptable_map(&ptable, (vaddr_t)data_begin, (vaddr_t)data_end,
		      (paddr_t)data_begin,
		      MM_MODE_R | MM_MODE_W | MM_MODE_NOSYNC | MM_MODE_STAGE1);

	arch_mm_init((paddr_t)ptable.table);

	/* TODO: Code below this point should be removed from this function. */
	do {
		struct fdt_node n;
		uint64_t mem_start = 0;
		uint64_t mem_size = 0;
		uint64_t new_mem_size;

		/* Map in the fdt header. */
		if (!mm_ptable_map(&ptable, (vaddr_t)fdt,
				   (vaddr_t)fdt + fdt_header_size(),
				   (paddr_t)fdt, MM_MODE_R | MM_MODE_STAGE1)) {
			dlog("Unable to map FDT header.\n");
			break;
		}

		/*
		 * Map the rest of the fdt plus an extra page for adding new
		 * memory reservations.
		 */
		if (!mm_ptable_map(&ptable, (vaddr_t)fdt,
				   (vaddr_t)fdt + fdt_total_size(fdt),
				   (paddr_t)fdt, MM_MODE_R | MM_MODE_STAGE1)) {
			dlog("Unable to map FDT.\n");
			break;
		}

		fdt_root_node(&n, fdt);
		fdt_find_child(&n, "");

		find_memory_range(&n, &mem_start, &mem_size);
		dlog("Memory range: 0x%x - 0x%x\n", mem_start,
		     mem_start + mem_size - 1);

		uint64_t begin;
		uint64_t end;

		if (!find_initrd(&n, &begin, &end)) {
			break;
		}

		dlog("Ramdisk range: 0x%x - 0x%x\n", begin, end - 1);
		mm_ptable_map(&ptable, begin, end, begin,
			      MM_MODE_R | MM_MODE_STAGE1);

		struct cpio c;
		cpio_init(&c, (void *)begin, end - begin);

		/* Map the fdt in r/w mode in preparation for extending it. */
		if (!mm_ptable_map(
			    &ptable, (vaddr_t)fdt,
			    (vaddr_t)fdt + fdt_total_size(fdt) + PAGE_SIZE,
			    (paddr_t)fdt,
			    MM_MODE_R | MM_MODE_W | MM_MODE_STAGE1)) {
			dlog("Unable to map FDT in r/w mode.\n");
			break;
		}
		new_mem_size = mem_size;
		load_secondary(&c, mem_start, &new_mem_size);
		load_primary(&c, &n);

		/* Patch fdt to reserve memory for secondary VMs. */
		fdt_add_mem_reservation(fdt, mem_start + new_mem_size,
					mem_size - new_mem_size);

		/* Unmap FDT. */
		if (!mm_ptable_unmap(
			    &ptable, (vaddr_t)fdt,
			    (vaddr_t)fdt + fdt_total_size(fdt) + PAGE_SIZE,
			    MM_MODE_STAGE1)) {
			dlog("Unable to unmap the FDT.\n");
			break;
		}
	} while (0);

	mm_ptable_defrag(&ptable);

	arch_set_vm_mm(&primary_vm.page_table);
}

/**
 * The entry point of CPUs when they are turned on. It is supposed to initialise
 * all state and return the first vCPU to run.
 */
struct vcpu *cpu_main(void)
{
	struct cpu *c = cpu();

	/* Do global one-time initialization just once. We avoid using atomics
	 * by only touching the variable from cpu 0. */
	static volatile bool inited = false;
	if (cpu_index(c) == 0 && !inited) {
		inited = true;
		one_time_init();
	}

	dlog("Starting up cpu %d\n", cpu_index(c));

	return primary_vm.vcpus + cpu_index(c);
}
