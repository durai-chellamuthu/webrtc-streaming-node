#!/usr/bin/env python
# Copyright 2012 The Swarming Authors. All rights reserved.
# Use of this source code is governed under the Apache License, Version 2.0 that
# can be found in the LICENSE file.

"""Reads a .isolated, creates a tree of hardlinks and runs the test.

To improve performance, it keeps a local cache. The local cache can safely be
deleted.

Any ${ISOLATED_OUTDIR} on the command line will be replaced by the location of a
temporary directory upon execution of the command specified in the .isolated
file. All content written to this directory will be uploaded upon termination
and the .isolated file describing this directory will be printed to stdout.
"""

__version__ = '0.5.2'

import logging
import optparse
import os
import sys
import tempfile

from third_party.depot_tools import fix_encoding

from utils import file_path
from utils import logging_utils
from utils import on_error
from utils import subprocess42
from utils import tools
from utils import zip_package

import auth
import isolated_format
import isolateserver


# Absolute path to this file (can be None if running from zip on Mac).
THIS_FILE_PATH = os.path.abspath(__file__) if __file__ else None

# Directory that contains this file (might be inside zip package).
BASE_DIR = os.path.dirname(THIS_FILE_PATH) if __file__ else None

# Directory that contains currently running script file.
if zip_package.get_main_script_path():
  MAIN_DIR = os.path.dirname(
      os.path.abspath(zip_package.get_main_script_path()))
else:
  # This happens when 'import run_isolated' is executed at the python
  # interactive prompt, in that case __file__ is undefined.
  MAIN_DIR = None

# The name of the log file to use.
RUN_ISOLATED_LOG_FILE = 'run_isolated.log'

# The name of the log to use for the run_test_cases.py command
RUN_TEST_CASES_LOG = 'run_test_cases.log'


def get_as_zip_package(executable=True):
  """Returns ZipPackage with this module and all its dependencies.

  If |executable| is True will store run_isolated.py as __main__.py so that
  zip package is directly executable be python.
  """
  # Building a zip package when running from another zip package is
  # unsupported and probably unneeded.
  assert not zip_package.is_zipped_module(sys.modules[__name__])
  assert THIS_FILE_PATH
  assert BASE_DIR
  package = zip_package.ZipPackage(root=BASE_DIR)
  package.add_python_file(THIS_FILE_PATH, '__main__.py' if executable else None)
  package.add_python_file(os.path.join(BASE_DIR, 'isolated_format.py'))
  package.add_python_file(os.path.join(BASE_DIR, 'isolateserver.py'))
  package.add_python_file(os.path.join(BASE_DIR, 'auth.py'))
  package.add_directory(os.path.join(BASE_DIR, 'third_party'))
  package.add_directory(os.path.join(BASE_DIR, 'utils'))
  return package


def make_temp_dir(prefix, root_dir=None):
  """Returns a temporary directory.

  If root_dir is given and /tmp is on same file system as root_dir, uses /tmp.
  Otherwise makes a new temp directory under root_dir.
  """
  base_temp_dir = None
  if (root_dir and
      not file_path.is_same_filesystem(root_dir, tempfile.gettempdir())):
    base_temp_dir = root_dir
  return unicode(tempfile.mkdtemp(prefix=prefix, dir=base_temp_dir))


def change_tree_read_only(rootdir, read_only):
  """Changes the tree read-only bits according to the read_only specification.

  The flag can be 0, 1 or 2, which will affect the possibility to modify files
  and create or delete files.
  """
  if read_only == 2:
    # Files and directories (except on Windows) are marked read only. This
    # inhibits modifying, creating or deleting files in the test directory,
    # except on Windows where creating and deleting files is still possible.
    file_path.make_tree_read_only(rootdir)
  elif read_only == 1:
    # Files are marked read only but not the directories. This inhibits
    # modifying files but creating or deleting files is still possible.
    file_path.make_tree_files_read_only(rootdir)
  elif read_only in (0, None):
    # Anything can be modified.
    # TODO(maruel): This is currently dangerous as long as DiskCache.touch()
    # is not yet changed to verify the hash of the content of the files it is
    # looking at, so that if a test modifies an input file, the file must be
    # deleted.
    file_path.make_tree_writeable(rootdir)
  else:
    raise ValueError(
        'change_tree_read_only(%s, %s): Unknown flag %s' %
        (rootdir, read_only, read_only))


def process_command(command, out_dir):
  """Replaces isolated specific variables in a command line."""
  def fix(arg):
    if '${ISOLATED_OUTDIR}' in arg:
      return arg.replace('${ISOLATED_OUTDIR}', out_dir).replace('/', os.sep)
    return arg

  return [fix(arg) for arg in command]


def run_command(command, cwd, tmp_dir):
  """Runs the command, returns the process exit code."""
  logging.info('run_command(%s, %s)' % (command, cwd))
  sys.stdout.flush()

  env = os.environ.copy()
  if sys.platform == 'darwin':
    env['TMPDIR'] = tmp_dir.encode('ascii')
  elif sys.platform == 'win32':
    # Temporarily disable this behavior on Windows while investigating
    # https://crbug.com/533552.
    # env['TEMP'] = tmp_dir.encode('ascii')
    pass
  else:
    env['TMP'] = tmp_dir.encode('ascii')
  with tools.Profiler('RunTest'):
    try:
      with subprocess42.Popen_with_handler(command, cwd=cwd, env=env) as p:
        p.communicate()
        exit_code = p.returncode
    except OSError:
      # This is not considered to be an internal error. The executable simply
      # does not exit.
      exit_code = 1
  logging.info(
      'Command finished with exit code %d (%s)',
      exit_code, hex(0xffffffff & exit_code))
  return exit_code


def delete_and_upload(storage, out_dir, leak_temp_dir):
  """Deletes the temporary run directory and uploads results back.

  Returns:
    tuple(outputs_ref, success)
    - outputs_ref is a dict referring to the results archived back to the
      isolated server, if applicable.
    - success is False if something occurred that means that the task must
      forcibly be considered a failure, e.g. zombie processes were left behind.
  """

  # Upload out_dir and generate a .isolated file out of this directory. It is
  # only done if files were written in the directory.
  outputs_ref = None
  if os.path.isdir(out_dir) and os.listdir(out_dir):
    with tools.Profiler('ArchiveOutput'):
      try:
        results = isolateserver.archive_files_to_storage(
            storage, [out_dir], None)
        outputs_ref = {
          'isolated': results[0][0],
          'isolatedserver': storage.location,
          'namespace': storage.namespace,
        }
      except isolateserver.Aborted:
        # This happens when a signal SIGTERM was received while uploading data.
        # There is 2 causes:
        # - The task was too slow and was about to be killed anyway due to
        #   exceeding the hard timeout.
        # - The amount of data uploaded back is very large and took too much
        #   time to archive.
        sys.stderr.write('Received SIGTERM while uploading')
        # Re-raise, so it will be treated as an internal failure.
        raise
  try:
    if (not leak_temp_dir and os.path.isdir(out_dir) and
        not file_path.rmtree(out_dir)):
      logging.error('Had difficulties removing out_dir %s', out_dir)
      return outputs_ref, False
  except OSError as e:
    # When this happens, it means there's a process error.
    logging.error('Had difficulties removing out_dir %s: %s', out_dir, e)
    return outputs_ref, False
  return outputs_ref, True


def map_and_run(
    isolated_hash, storage, cache, leak_temp_dir, root_dir, extra_args):
  """Maps and run the command. Returns metadata about the result."""
  # TODO(maruel): Include performance statistics.
  result = {
    'exit_code': None,
    'internal_failure': None,
    'outputs_ref': None,
    'version': 1,
  }
  if root_dir:
    if not os.path.isdir(root_dir):
      os.makedirs(root_dir, 0700)
    prefix = u''
  else:
    root_dir = os.path.dirname(cache.cache_dir) if cache.cache_dir else None
    prefix = u'isolated_'
  run_dir = make_temp_dir(prefix + u'run', root_dir)
  out_dir = make_temp_dir(prefix + u'out', root_dir)
  tmp_dir = make_temp_dir(prefix + u'tmp', root_dir)
  try:
    bundle = isolateserver.fetch_isolated(
        isolated_hash=isolated_hash,
        storage=storage,
        cache=cache,
        outdir=run_dir,
        require_command=True)

    change_tree_read_only(run_dir, bundle.read_only)
    cwd = os.path.normpath(os.path.join(run_dir, bundle.relative_cwd))
    command = bundle.command + extra_args
    file_path.ensure_command_has_abs_path(command, cwd)
    result['exit_code'] = run_command(
        process_command(command, out_dir), cwd, tmp_dir)
  except Exception as e:
    # An internal error occured. Report accordingly so the swarming task will be
    # retried automatically.
    logging.error('internal failure: %s', e)
    result['internal_failure'] = str(e)
    on_error.report(None)
  finally:
    try:
      if leak_temp_dir:
        logging.warning(
            'Deliberately leaking %s for later examination', run_dir)
      else:
        if os.path.isdir(run_dir) and not file_path.rmtree(run_dir):
          # On Windows rmtree(run_dir) call above has a synchronization effect:
          # it finishes only when all task child processes terminate (since a
          # running process locks *.exe file). Examine out_dir only after that
          # call completes (since child processes may write to out_dir too and
          # we need to wait for them to finish).
          print >> sys.stderr, (
              'Failed to delete the run directory, forcibly failing\n'
              'the task because of it. No zombie process can outlive a\n'
              'successful task run and still be marked as successful.\n'
              'Fix your stuff.')
          if result['exit_code'] == 0:
            result['exit_code'] = 1
        if os.path.isdir(tmp_dir) and not file_path.rmtree(tmp_dir):
          print >> sys.stderr, (
              'Failed to delete the temporary directory, forcibly failing\n'
              'the task because of it. No zombie process can outlive a\n'
              'successful task run and still be marked as successful.\n'
              'Fix your stuff.')
          if result['exit_code'] == 0:
            result['exit_code'] = 1

      # This deletes out_dir if leak_temp_dir is not set.
      result['outputs_ref'], success = delete_and_upload(
          storage, out_dir, leak_temp_dir)
      if not success and result['exit_code'] == 0:
        result['exit_code'] = 1
    except Exception as e:
      # Swallow any exception in the main finally clause.
      logging.error('Leaking out_dir %s: %s', out_dir, e)
      result['internal_failure'] = str(e)
  return result


def run_tha_test(
    isolated_hash, storage, cache, leak_temp_dir, result_json, root_dir,
    extra_args):
  """Downloads the dependencies in the cache, hardlinks them into a temporary
  directory and runs the executable from there.

  A temporary directory is created to hold the output files. The content inside
  this directory will be uploaded back to |storage| packaged as a .isolated
  file.

  Arguments:
    isolated_hash: the SHA-1 of the .isolated file that must be retrieved to
                   recreate the tree of files to run the target executable.
    storage: an isolateserver.Storage object to retrieve remote objects. This
             object has a reference to an isolateserver.StorageApi, which does
             the actual I/O.
    cache: an isolateserver.LocalCache to keep from retrieving the same objects
           constantly by caching the objects retrieved. Can be on-disk or
           in-memory.
    leak_temp_dir: if true, the temporary directory will be deliberately leaked
                   for later examination.
    result_json: file path to dump result metadata into. If set, the process
                 exit code is always 0 unless an internal error occured.
    root_dir: directory to the path to use to create the temporary directory. If
              not specified, a random temporary directory is created.
    extra_args: optional arguments to add to the command stated in the .isolate
                file.

  Returns:
    Process exit code that should be used.
  """
  # run_isolated exit code. Depends on if result_json is used or not.
  result = map_and_run(
      isolated_hash, storage, cache, leak_temp_dir, root_dir, extra_args)
  logging.info('Result:\n%s', tools.format_json(result, dense=True))
  if result_json:
    # We've found tests to delete 'work' when quitting, causing an exception
    # here. Try to recreate the directory if necessary.
    work_dir = os.path.dirname(result_json)
    if not os.path.isdir(work_dir):
      os.mkdir(work_dir)
    tools.write_json(result_json, result, dense=True)
    # Only return 1 if there was an internal error.
    return int(bool(result['internal_failure']))

  # Marshall into old-style inline output.
  if result['outputs_ref']:
    data = {
      'hash': result['outputs_ref']['isolated'],
      'namespace': result['outputs_ref']['namespace'],
      'storage': result['outputs_ref']['isolatedserver'],
    }
    sys.stdout.flush()
    print(
        '[run_isolated_out_hack]%s[/run_isolated_out_hack]' %
        tools.format_json(data, dense=True))
  return result['exit_code'] or int(bool(result['internal_failure']))


def main(args):
  tools.disable_buffering()
  parser = logging_utils.OptionParserWithLogging(
      usage='%prog <options>',
      version=__version__,
      log_file=RUN_ISOLATED_LOG_FILE)
  parser.add_option(
      '--json',
      help='dump output metadata to json file. When used, run_isolated returns '
           'non-zero only on internal failure')
  data_group = optparse.OptionGroup(parser, 'Data source')
  data_group.add_option(
      '-s', '--isolated',
      help='Hash of the .isolated to grab from the isolate server')
  isolateserver.add_isolate_server_options(data_group)
  parser.add_option_group(data_group)

  isolateserver.add_cache_options(parser)
  parser.set_defaults(cache='cache')

  debug_group = optparse.OptionGroup(parser, 'Debugging')
  debug_group.add_option(
      '--leak-temp-dir',
      action='store_true',
      help='Deliberately leak isolate\'s temp dir for later examination '
          '[default: %default]')
  debug_group.add_option(
      '--root-dir', help='Use a directory instead of a random one')
  parser.add_option_group(debug_group)

  auth.add_auth_options(parser)
  options, args = parser.parse_args(args)
  if not options.isolated:
    parser.error('--isolated is required.')
  auth.process_auth_options(parser, options)
  isolateserver.process_isolate_server_options(parser, options, True)

  cache = isolateserver.process_cache_options(options)
  with isolateserver.get_storage(
      options.isolate_server, options.namespace) as storage:
    # Hashing schemes used by |storage| and |cache| MUST match.
    assert storage.hash_algo == cache.hash_algo
    return run_tha_test(
        options.isolated, storage, cache, options.leak_temp_dir, options.json,
        options.root_dir, args)


if __name__ == '__main__':
  # Ensure that we are always running with the correct encoding.
  fix_encoding.fix_encoding()
  sys.exit(main(sys.argv[1:]))