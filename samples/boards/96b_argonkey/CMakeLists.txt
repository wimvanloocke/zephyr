cmake_minimum_required(VERSION 3.13.1)
# Copyright (c) 2018 STMicroelectronics
#
# SPDX-License-Identifier: Apache-2.0
#

include($ENV{ZEPHYR_BASE}/cmake/app/boilerplate.cmake NO_POLICY_SCOPE)
project(96b_argonkey)

target_sources(app PRIVATE src/main.c)
