# Copyright 2014 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

python_library(
  name = 'mock_logger',
  sources = globs('mock_logger.py'),
  dependencies = [
    '3rdparty/python:future',
    'src/python/pants/reporting',
  ],
)

python_library(
  name = 'file_test_util',
  sources = [
    'file_test_util.py',
  ],
)

python_library(
  name = 'git_util',
  sources = [
    'git_util.py',
  ],
  dependencies = [
    'src/python/pants/base:revision',
    'src/python/pants/scm:git',
    'src/python/pants/util:contextutil',
    'src/python/pants/util:process_handler',
  ],
)

python_library(
  name = 'process_test_util',
  sources = [
    'process_test_util.py',
  ],
  dependencies = [
    '3rdparty/python:psutil',
  ],
)

python_library(
  name = 'pexrc_util',
  sources = [
    'pexrc_util.py',
  ],
  dependencies = [
    ':git_util'
  ,]
)
