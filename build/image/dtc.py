#!/usr/bin/env python3
#
# Copyright 2019 The Hafnium Authors.
#
# Use of this source code is governed by a BSD-style
# license that can be found in the LICENSE file or at
# https://opensource.org/licenses/BSD-3-Clause.

"""Wrapper around Device Tree Compiler (dtc)"""

import argparse
import os
import subprocess
import sys

HF_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
DTC_ROOT = os.path.join(HF_ROOT, "prebuilts", "linux-x64", "dtc")
DTC = os.path.join(DTC_ROOT, "dtc")
FDTOVERLAY = os.path.join(DTC_ROOT, "fdtoverlay")

def cmd_compile(args):
    exec_args = [
            DTC,
            "-I", "dts", "-O", "dtb",
            "--out-version", "17",
        ]

    if args.output_file:
        exec_args += [ "-o", args.output_file ]
    if args.input_file:
        exec_args += [ args.input_file ]

    return subprocess.call(exec_args)

def cmd_overlay(args):
    exec_args = [
            FDTOVERLAY,
            "-i", args.base_dtb,
            "-o", args.output_dtb,
        ] + args.overlay_dtb
    return subprocess.call(exec_args)

def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")

    parser_compile = subparsers.add_parser("compile", help="compile DTS to DTB")
    parser_compile.add_argument("-i", "--input-file")
    parser_compile.add_argument("-o", "--output-file")

    parser_overlay = subparsers.add_parser("overlay", help="merge DTBs")
    parser_overlay.add_argument("output_dtb")
    parser_overlay.add_argument("base_dtb")
    parser_overlay.add_argument("overlay_dtb", nargs='*')

    args = parser.parse_args()

    if args.command == "compile":
        return cmd_compile(args)
    elif args.command == "overlay":
        return cmd_overlay(args)
    else:
        raise Error("Unknown command: {}".format(args.command))

if __name__ == "__main__":
    sys.exit(main())
