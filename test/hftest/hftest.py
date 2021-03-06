#!/usr/bin/env python3
#
# Copyright 2018 The Hafnium Authors.
#
# Use of this source code is governed by a BSD-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/BSD-3-Clause.

"""Script which drives invocation of tests and parsing their output to produce
a results report.
"""

from __future__ import print_function

import xml.etree.ElementTree as ET

import argparse
import collections
import datetime
import importlib
import json
import os
import re
import subprocess
import sys
import time

HFTEST_LOG_PREFIX = "[hftest] "
HFTEST_LOG_FAILURE_PREFIX = "Failure:"
HFTEST_LOG_FINISHED = "FINISHED"

HFTEST_CTRL_GET_COMMAND_LINE = "[hftest_ctrl:get_command_line]"
HFTEST_CTRL_FINISHED = "[hftest_ctrl:finished]"

HF_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
DTC_SCRIPT = os.path.join(HF_ROOT, "build", "image", "dtc.py")
FVP_BINARY = os.path.join(
    os.path.dirname(HF_ROOT), "fvp", "Base_RevC_AEMv8A_pkg", "models",
    "Linux64_GCC-6.4", "FVP_Base_RevC-2xAEMv8A")
FVP_PREBUILTS_ROOT = os.path.join(
    HF_ROOT, "prebuilts", "linux-aarch64", "trusted-firmware-a-trusty", "fvp")
FVP_PREBUILT_DTS = os.path.join(
    FVP_PREBUILTS_ROOT, "fvp-base-gicv3-psci-1t.dts")
FVP_PREBUILT_BL31 = os.path.join(FVP_PREBUILTS_ROOT, "bl31.bin")

def read_file(path):
    with open(path, "r") as f:
        return f.read()

def write_file(path, to_write, append=False):
    with open(path, "a" if append else "w") as f:
        f.write(to_write)

def append_file(path, to_write):
    write_file(path, to_write, append=True)

def join_if_not_None(*args):
    return " ".join(filter(lambda x: x, args))

class ArtifactsManager:
    """Class which manages folder with test artifacts."""

    def __init__(self, log_dir):
        self.created_files = []
        self.log_dir = log_dir

        # Create directory.
        try:
            os.makedirs(self.log_dir)
        except OSError:
            if not os.path.isdir(self.log_dir):
                raise
        print("Logs saved under", log_dir)

        # Create files expected by the Sponge test result parser.
        self.sponge_log_path = self.create_file("sponge_log", ".log")
        self.sponge_xml_path = self.create_file("sponge_log", ".xml")

    def gen_file_path(self, basename, extension):
        """Generate path to a file in the log directory."""
        return os.path.join(self.log_dir, basename + extension)

    def create_file(self, basename, extension):
        """Create and touch a new file in the log folder. Ensure that no other
        file of the same name was created by this instance of ArtifactsManager.
        """
        # Determine the path of the file.
        path = self.gen_file_path(basename, extension)

        # Check that the path is unique.
        assert(path not in self.created_files)
        self.created_files += [ path ]

        # Touch file.
        with open(path, "w") as f:
            pass

        return path

    def get_file(self, basename, extension):
        """Return path to a file in the log folder. Assert that it was created
        by this instance of ArtifactsManager."""
        path = self.gen_file_path(basename, extension)
        assert(path in self.created_files)
        return path


# Tuple holding the arguments common to all driver constructors.
# This is to avoid having to pass arguments from subclasses to superclasses.
DriverArgs = collections.namedtuple("DriverArgs", [
        "artifacts",
        "kernel",
        "initrd",
        "vm_args",
        "cpu",
    ])


# State shared between the common Driver class and its subclasses during
# a single invocation of the target platform.
class DriverRunState:
    def __init__(self, log_path):
        self.log_path = log_path
        self.ret_code = 0

    def set_ret_code(self, ret_code):
        self.ret_code = ret_code

class DriverRunException(Exception):
    """Exception thrown if subprocess invoked by a driver returned non-zero
    status code. Used to fast-exit from a driver command sequence."""
    pass


class Driver:
    """Parent class of drivers for all testable platforms."""

    def __init__(self, args):
        self.args = args

    def get_run_log(self, run_name):
        """Return path to the main log of a given test run."""
        return self.args.artifacts.get_file(run_name, ".log")

    def start_run(self, run_name):
        """Hook called by Driver subclasses before they invoke the target
        platform."""
        return DriverRunState(self.args.artifacts.create_file(run_name, ".log"))

    def exec_logged(self, run_state, exec_args, cwd=None):
        """Run a subprocess on behalf of a Driver subclass and append its
        stdout and stderr to the main log."""
        assert(run_state.ret_code == 0)
        with open(run_state.log_path, "a") as f:
            f.write("$ {}\r\n".format(" ".join(exec_args)))
            f.flush()
            ret_code = subprocess.call(exec_args, stdout=f, stderr=f, cwd=cwd)
            if ret_code != 0:
                run_state.set_ret_code(ret_code)
                raise DriverRunException()

    def finish_run(self, run_state):
        """Hook called by Driver subclasses after they finished running the
        target platform. `ret_code` argument is the return code of the main
        command run by the driver. A corresponding log message is printed."""
        # Decode return code and add a message to the log.
        with open(run_state.log_path, "a") as f:
            if run_state.ret_code == 124:
                f.write("\r\n{}{} timed out\r\n".format(
                    HFTEST_LOG_PREFIX, HFTEST_LOG_FAILURE_PREFIX))
            elif run_state.ret_code != 0:
                f.write("\r\n{}{} process return code {}\r\n".format(
                    HFTEST_LOG_PREFIX, HFTEST_LOG_FAILURE_PREFIX,
                    run_state.ret_code))

        # Append log of this run to full test log.
        log_content = read_file(run_state.log_path)
        append_file(
            self.args.artifacts.sponge_log_path,
            log_content + "\r\n\r\n")
        return log_content


class QemuDriver(Driver):
    """Driver which runs tests in QEMU."""

    def __init__(self, args, qemu_wd, tfa):
        Driver.__init__(self, args)
        self.qemu_wd = qemu_wd
        self.tfa = tfa

    def gen_exec_args(self, test_args, is_long_running):
        """Generate command line arguments for QEMU."""
        time_limit = "120s" if is_long_running else "10s"
        # If no CPU configuration is selected, then test against the maximum
        # configuration, "max", supported by QEMU.
        cpu = self.args.cpu or "max"
        exec_args = [
            "timeout", "--foreground", time_limit,
            os.path.abspath("prebuilts/linux-x64/qemu/qemu-system-aarch64"),
            "-machine", "virt,virtualization=on,gic-version=3",
            "-cpu", cpu, "-smp", "4", "-m", "1G",
            "-nographic", "-nodefaults", "-serial", "stdio",
            "-d", "unimp", "-kernel", os.path.abspath(self.args.kernel),
        ]

        if self.tfa:
            exec_args += ["-bios",
                os.path.abspath(
                    "prebuilts/linux-aarch64/trusted-firmware-a-trusty/qemu/bl1.bin"
                ), "-machine", "secure=on", "-semihosting-config",
                "enable,target=native"]

        if self.args.initrd:
            exec_args += ["-initrd", os.path.abspath(self.args.initrd)]

        vm_args = join_if_not_None(self.args.vm_args, test_args)
        if vm_args:
            exec_args += ["-append", vm_args]

        return exec_args

    def run(self, run_name, test_args, is_long_running):
        """Run test given by `test_args` in QEMU."""
        run_state = self.start_run(run_name)

        try:
            # Execute test in QEMU..
            exec_args = self.gen_exec_args(test_args, is_long_running)
            self.exec_logged(run_state, exec_args,
                cwd=self.qemu_wd)
        except DriverRunException:
            pass

        return self.finish_run(run_state)

    def finish(self):
        """Clean up after running tests."""
        pass


class FvpDriver(Driver):
    """Driver which runs tests in Arm FVP emulator."""

    def __init__(self, args):
        if args.cpu:
            raise ValueError("FVP emulator does not support the --cpu option.")
        Driver.__init__(self, args)

    def gen_dts(self, dts_path, test_args, initrd_start, initrd_end):
        """Create a DeviceTree source which will be compiled into a DTB and
        passed to FVP for a test run."""
        vm_args = join_if_not_None(self.args.vm_args, test_args)
        write_file(dts_path, read_file(FVP_PREBUILT_DTS))
        append_file(dts_path, """
                / {{
                    chosen {{
                        bootargs = "{}";
                        stdout-path = "serial0:115200n8";
                        linux,initrd-start = <{}>;
                        linux,initrd-end = <{}>;
                    }};
                }};
            """.format(vm_args, initrd_start, initrd_end))

    def gen_fvp_args(
            self, is_long_running, initrd_start, uart0_log_path, uart1_log_path,
            dtb_path):
        """Generate command line arguments for FVP."""
        time_limit = "80s" if is_long_running else "40s"
        fvp_args = [
            "timeout", "--foreground", time_limit,
            FVP_BINARY,
            "-C", "pctl.startup=0.0.0.0",
            "-C", "bp.secure_memory=0",
            "-C", "cluster0.NUM_CORES=4",
            "-C", "cluster1.NUM_CORES=4",
            "-C", "cache_state_modelled=0",
            "-C", "bp.vis.disable_visualisation=true",
            "-C", "bp.vis.rate_limit-enable=false",
            "-C", "bp.terminal_0.start_telnet=false",
            "-C", "bp.terminal_1.start_telnet=false",
            "-C", "bp.terminal_2.start_telnet=false",
            "-C", "bp.terminal_3.start_telnet=false",
            "-C", "bp.pl011_uart0.untimed_fifos=1",
            "-C", "bp.pl011_uart0.unbuffered_output=1",
            "-C", "bp.pl011_uart0.out_file=" + uart0_log_path,
            "-C", "bp.pl011_uart1.out_file=" + uart1_log_path,
            "-C", "cluster0.cpu0.RVBAR=0x04020000",
            "-C", "cluster0.cpu1.RVBAR=0x04020000",
            "-C", "cluster0.cpu2.RVBAR=0x04020000",
            "-C", "cluster0.cpu3.RVBAR=0x04020000",
            "-C", "cluster1.cpu0.RVBAR=0x04020000",
            "-C", "cluster1.cpu1.RVBAR=0x04020000",
            "-C", "cluster1.cpu2.RVBAR=0x04020000",
            "-C", "cluster1.cpu3.RVBAR=0x04020000",
            "--data", "cluster0.cpu0=" + FVP_PREBUILT_BL31 + "@0x04020000",
            "--data", "cluster0.cpu0=" + dtb_path + "@0x82000000",
            "--data", "cluster0.cpu0=" + self.args.kernel + "@0x80000000",
            "-C", "bp.ve_sysregs.mmbSiteDefault=0",
            "-C", "bp.ve_sysregs.exit_on_shutdown=1",
        ]

        if self.args.initrd:
            fvp_args += [
                "--data",
                "cluster0.cpu0={}@{}".format(
                    self.args.initrd, hex(initrd_start))
            ]

        return fvp_args

    def run(self, run_name, test_args, is_long_running):
        run_state = self.start_run(run_name)

        dts_path = self.args.artifacts.create_file(run_name, ".dts")
        dtb_path = self.args.artifacts.create_file(run_name, ".dtb")
        uart0_log_path = self.args.artifacts.create_file(run_name, ".uart0.log")
        uart1_log_path = self.args.artifacts.create_file(run_name, ".uart1.log")

        initrd_start = 0x84000000
        if self.args.initrd:
            initrd_end = initrd_start + os.path.getsize(self.args.initrd)
        else:
            initrd_end = 0x85000000  # Default value

        try:
            # Create a DT to pass to FVP.
            self.gen_dts(dts_path, test_args, initrd_start, initrd_end)

            # Compile DTS to DTB.
            dtc_args = [
                DTC_SCRIPT, "compile", "-i", dts_path, "-o", dtb_path,
            ]
            self.exec_logged(run_state, dtc_args)

            # Run FVP.
            fvp_args = self.gen_fvp_args(
                is_long_running, initrd_start, uart0_log_path, uart1_log_path,
                dtb_path)
            self.exec_logged(run_state, fvp_args)
        except DriverRunException:
            pass

        # Append UART0 output to main log.
        append_file(run_state.log_path, read_file(uart0_log_path))
        return self.finish_run(run_state)

    def finish(self):
        """Clean up after running tests."""
        pass


class SerialDriver(Driver):
    """Driver which communicates with a device over the serial port."""

    def __init__(self, args, tty_file, baudrate, init_wait):
        Driver.__init__(self, args)
        self.tty_file = tty_file
        self.baudrate = baudrate
        self.pyserial = importlib.import_module("serial")

        if init_wait:
            input("Press ENTER and then reset the device...")

    def connect(self):
        return self.pyserial.Serial(self.tty_file, self.baudrate, timeout=10)

    def run(self, run_name, test_args, is_long_running):
        """Communicate `test_args` to the device over the serial port."""
        run_state = self.start_run(run_name)

        with self.connect() as ser:
            with open(run_state.log_path, "a") as f:
                while True:
                    # Read one line from the serial port.
                    line = ser.readline().decode('utf-8')
                    if len(line) == 0:
                        # Timeout
                        run_state.set_ret_code(124)
                        input("Timeout. " +
                            "Press ENTER and then reset the device...")
                        break
                    # Write the line to the log file.
                    f.write(line)
                    if HFTEST_CTRL_GET_COMMAND_LINE in line:
                        # Device is waiting for `test_args`.
                        ser.write(test_args.encode('ascii'))
                        ser.write(b'\r')
                    elif HFTEST_CTRL_FINISHED in line:
                        # Device has finished running this test and will reboot.
                        break
        return self.finish_run(run_state)

    def finish(self):
        """Clean up after running tests."""
        with self.connect() as ser:
            while True:
                line = ser.readline().decode('utf-8')
                if len(line) == 0:
                    input("Timeout. Press ENTER and then reset the device...")
                elif HFTEST_CTRL_GET_COMMAND_LINE in line:
                    # Device is waiting for a command. Instruct it to exit
                    # the test environment.
                    ser.write("exit".encode('ascii'))
                    ser.write(b'\r')
                    break


# Tuple used to return information about the results of running a set of tests.
TestRunnerResult = collections.namedtuple("TestRunnerResult", [
        "tests_run",
        "tests_failed",
        "tests_skipped",
    ])


class TestRunner:
    """Class which communicates with a test platform to obtain a list of
    available tests and driving their execution."""

    def __init__(self, artifacts, driver, image_name, suite_regex, test_regex,
            skip_long_running_tests, force_long_running):
        self.artifacts = artifacts
        self.driver = driver
        self.image_name = image_name
        self.skip_long_running_tests = skip_long_running_tests
        self.force_long_running = force_long_running

        self.suite_re = re.compile(suite_regex or ".*")
        self.test_re = re.compile(test_regex or ".*")

    def extract_hftest_lines(self, raw):
        """Extract hftest-specific lines from a raw output from an invocation
        of the test platform."""
        lines = []
        for line in raw.splitlines():
            if line.startswith("VM "):
                line = line[len("VM 0: "):]
            if line.startswith(HFTEST_LOG_PREFIX):
                lines.append(line[len(HFTEST_LOG_PREFIX):])
        return lines

    def get_test_json(self):
        """Invoke the test platform and request a JSON of available test and
        test suites."""
        out = self.driver.run("json", "json", self.force_long_running)
        hf_out = "\n".join(self.extract_hftest_lines(out))
        try:
            return json.loads(hf_out)
        except ValueError as e:
            print(out)
            raise e

    def collect_results(self, fn, it, xml_node):
        """Run `fn` on every entry in `it` and collect their TestRunnerResults.
        Insert "tests" and "failures" nodes to `xml_node`."""
        tests_run = 0
        tests_failed = 0
        tests_skipped = 0
        start_time = time.perf_counter()
        for i in it:
            sub_result = fn(i)
            assert(sub_result.tests_run >= sub_result.tests_failed)
            tests_run += sub_result.tests_run
            tests_failed += sub_result.tests_failed
            tests_skipped += sub_result.tests_skipped
        elapsed_time = time.perf_counter() - start_time

        xml_node.set("tests", str(tests_run + tests_skipped))
        xml_node.set("failures", str(tests_failed))
        xml_node.set("skipped", str(tests_skipped))
        xml_node.set("time", str(elapsed_time))
        return TestRunnerResult(tests_run, tests_failed, tests_skipped)

    def is_passed_test(self, test_out):
        """Parse the output of a test and return True if it passed."""
        return \
            len(test_out) > 0 and \
            test_out[-1] == HFTEST_LOG_FINISHED and \
            not any(l.startswith(HFTEST_LOG_FAILURE_PREFIX) for l in test_out)

    def get_failure_message(self, test_out):
        """Parse the output of a test and return the message of the first
        assertion failure."""
        for i, line in enumerate(test_out):
            if line.startswith(HFTEST_LOG_FAILURE_PREFIX) and i + 1 < len(test_out):
                # The assertion message is on the line after the 'Failure:'
                return test_out[i + 1].strip()

        return None

    def get_log_name(self, suite, test):
        """Returns a string with a generated log name for the test."""
        log_name = ""

        cpu = self.driver.args.cpu
        if cpu:
            log_name += cpu + "."

        log_name += suite["name"] + "." + test["name"]

        return log_name

    def run_test(self, suite, test, suite_xml):
        """Invoke the test platform and request to run a given `test` in given
        `suite`. Create a new XML node with results under `suite_xml`.
        Test only invoked if it matches the regex given to constructor."""
        if not self.test_re.match(test["name"]):
            return TestRunnerResult(tests_run=0, tests_failed=0, tests_skipped=0)

        test_xml = ET.SubElement(suite_xml, "testcase")
        test_xml.set("name", test["name"])
        test_xml.set("classname", suite["name"])

        if self.skip_long_running_tests and test["is_long_running"]:
            print("      SKIP", test["name"])
            test_xml.set("status", "notrun")
            skipped_xml = ET.SubElement(test_xml, "skipped")
            skipped_xml.set("message", "Long running")
            return TestRunnerResult(tests_run=0, tests_failed=0, tests_skipped=1)

        print("      RUN", test["name"])
        log_name = self.get_log_name(suite, test)

        test_xml.set("status", "run")

        start_time = time.perf_counter()
        out = self.driver.run(
            log_name, "run {} {}".format(suite["name"], test["name"]),
            test["is_long_running"] or self.force_long_running)
        hftest_out = self.extract_hftest_lines(out)
        elapsed_time = time.perf_counter() - start_time

        test_xml.set("time", str(elapsed_time))

        system_out_xml = ET.SubElement(test_xml, "system-out")
        system_out_xml.text = out

        if self.is_passed_test(hftest_out):
            print("        PASS")
            return TestRunnerResult(tests_run=1, tests_failed=0, tests_skipped=0)
        else:
            print("[x]     FAIL --", self.driver.get_run_log(log_name))
            failure_xml = ET.SubElement(test_xml, "failure")
            failure_message = self.get_failure_message(hftest_out) or "Test failed"
            failure_xml.set("message", failure_message)
            failure_xml.text = '\n'.join(hftest_out)
            return TestRunnerResult(tests_run=1, tests_failed=1, tests_skipped=0)

    def run_suite(self, suite, xml):
        """Invoke the test platform and request to run all matching tests in
        `suite`. Create new XML nodes with results under `xml`.
        Suite skipped if it does not match the regex given to constructor."""
        if not self.suite_re.match(suite["name"]):
            return TestRunnerResult(tests_run=0, tests_failed=0, tests_skipped=0)

        print("    SUITE", suite["name"])
        suite_xml = ET.SubElement(xml, "testsuite")
        suite_xml.set("name", suite["name"])
        properties_xml = ET.SubElement(suite_xml, "properties")

        property_xml = ET.SubElement(properties_xml, "property")
        property_xml.set("name", "driver")
        property_xml.set("value", type(self.driver).__name__)

        if self.driver.args.cpu:
            property_xml = ET.SubElement(properties_xml, "property")
            property_xml.set("name", "cpu")
            property_xml.set("value", self.driver.args.cpu)

        return self.collect_results(
            lambda test: self.run_test(suite, test, suite_xml),
            suite["tests"],
            suite_xml)

    def run_tests(self):
        """Run all suites and tests matching regexes given to constructor.
        Write results to sponge log XML. Return the number of run and failed
        tests."""

        test_spec = self.get_test_json()
        timestamp = datetime.datetime.now().replace(microsecond=0).isoformat()

        xml = ET.Element("testsuites")
        xml.set("name", self.image_name)
        xml.set("timestamp", timestamp)

        result = self.collect_results(
            lambda suite: self.run_suite(suite, xml),
            test_spec["suites"],
            xml)

        # Write XML to file.
        ET.ElementTree(xml).write(self.artifacts.sponge_xml_path,
            encoding='utf-8', xml_declaration=True)

        if result.tests_failed > 0:
            print("[x] FAIL:", result.tests_failed, "of", result.tests_run,
                    "tests failed")
        elif result.tests_run > 0:
            print("    PASS: all", result.tests_run, "tests passed")

        # Let the driver clean up.
        self.driver.finish()

        return result


def Main():
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("--out", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--out_initrd")
    parser.add_argument("--initrd")
    parser.add_argument("--suite")
    parser.add_argument("--test")
    parser.add_argument("--vm_args")
    parser.add_argument("--driver", default="qemu")
    parser.add_argument("--serial-dev", default="/dev/ttyUSB0")
    parser.add_argument("--serial-baudrate", type=int, default=115200)
    parser.add_argument("--serial-no-init-wait", action="store_true")
    parser.add_argument("--skip-long-running-tests", action="store_true")
    parser.add_argument("--force-long-running", action="store_true")
    parser.add_argument("--cpu",
        help="Selects the CPU configuration for the run environment.")
    parser.add_argument("--tfa", action="store_true")
    args = parser.parse_args()

    # Resolve some paths.
    image = os.path.join(args.out, args.image + ".bin")
    initrd = None
    image_name = args.image
    if args.initrd:
        initrd_dir = os.path.join(args.out_initrd, "obj", args.initrd)
        initrd = os.path.join(initrd_dir, "initrd.img")
        image_name += "_" + args.initrd
    vm_args = args.vm_args or ""

    # Create class which will manage all test artifacts.
    artifacts = ArtifactsManager(os.path.join(args.log, image_name))

    # Create a driver for the platform we want to test on.
    driver_args = DriverArgs(artifacts, image, initrd, vm_args, args.cpu)

    if args.driver == "qemu":
        driver = QemuDriver(driver_args, args.out, args.tfa)
    elif args.driver == "fvp":
        driver = FvpDriver(driver_args)
    elif args.driver == "serial":
        driver = SerialDriver(driver_args, args.serial_dev,
                args.serial_baudrate, not args.serial_no_init_wait)
    else:
        raise Exception("Unknown driver name: {}".format(args.driver))

    # Create class which will drive test execution.
    runner = TestRunner(artifacts, driver, image_name, args.suite, args.test,
        args.skip_long_running_tests, args.force_long_running)

    # Run tests.
    runner_result = runner.run_tests()

    # Print error message if no tests were run as this is probably unexpected.
    # Return suitable error code.
    if runner_result.tests_run == 0:
        print("Error: no tests match")
        return 10
    elif runner_result.tests_failed > 0:
        return 1
    else:
        return 0

if __name__ == "__main__":
    sys.exit(Main())
