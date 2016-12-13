"""Test main execution."""

import os
import logging

from contextlib import ExitStack, contextmanager
from io import StringIO
from pickle import load
from pkg_resources import resource_filename
from subprocess import CalledProcessError
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from ubuntu_image.__main__ import main, parseargs
from ubuntu_image.helpers import GiB, MiB
from ubuntu_image.testing.helpers import (
    CrashingModelAssertionBuilder, DoNothingBuilder,
    EarlyExitLeaveATraceAssertionBuilder, EarlyExitModelAssertionBuilder,
    LogCapture, XXXModelAssertionBuilder)
from ubuntu_image.testing.nose import NosePlugin
from unittest import TestCase, skipIf
from unittest.mock import call, patch


# For forcing a test failure.
def check_returncode(*args, **kws):
    raise CalledProcessError(1, 'failing command')


@contextmanager
def envar(key, value):
    missing = object()
    # Temporary set an environment variable.
    old_value = os.environ.get(key, missing)
    os.environ[key] = value
    try:
        yield
    finally:
        if old_value is missing:
            del os.environ[key]
        else:
            os.environ[key] = old_value


@contextmanager
def chdir(new_dir):
    here = os.getcwd()
    try:
        os.chdir(new_dir)
        yield
    finally:
        os.chdir(here)


class BadGadgetModelAssertionBuilder(XXXModelAssertionBuilder):
    gadget_yaml = 'bad-gadget.yaml'


class TestParseArgs(TestCase):
    def test_image_size_option_bytes(self):
        args = parseargs(['--image-size', '45', 'model.assertion'])
        self.assertEqual(args.image_size, 45)
        self.assertEqual(args.given_image_size, '45')

    def test_image_size_option_suffixes(self):
        args = parseargs(['--image-size', '45G', 'model.assertion'])
        self.assertEqual(args.image_size, GiB(45))
        self.assertEqual(args.given_image_size, '45G')
        args = parseargs(['--image-size', '45M', 'model.assertion'])
        self.assertEqual(args.image_size, MiB(45))
        self.assertEqual(args.given_image_size, '45M')

    def test_image_size_option_invalid(self):
        # These errors will output to stderr, but that just clouds the test
        # output, so suppress it.
        with patch('argparse._sys.stderr'):
            self.assertRaises(SystemExit,
                              parseargs,
                              ['--image-size', '45Q', 'model.assertion'])
            self.assertRaises(SystemExit,
                              parseargs,
                              ['--image-size', 'BIG', 'model.assertion'])


class TestMain(TestCase):
    def setUp(self):
        super().setUp()
        self._resources = ExitStack()
        self.addCleanup(self._resources.close)
        # Capture builtin print() output.
        self._stdout = StringIO()
        self._stderr = StringIO()
        self._resources.enter_context(
            patch('argparse._sys.stdout', self._stdout))
        # Capture stderr since this is where argparse will spew to.
        self._resources.enter_context(
            patch('argparse._sys.stderr', self._stderr))

    def test_help(self):
        with self.assertRaises(SystemExit) as cm:
            main(('--help',))
        self.assertEqual(cm.exception.code, 0)
        lines = self._stdout.getvalue().splitlines()
        self.assertTrue(lines[0].startswith('usage: ubuntu-image'),
                        lines[0])

    def test_debug(self):
        with ExitStack() as resources:
            mock = resources.enter_context(
                patch('ubuntu_image.__main__.logging.basicConfig'))
            resources.enter_context(patch(
                'ubuntu_image.__main__.ModelAssertionBuilder',
                EarlyExitModelAssertionBuilder))
            # Prevent actual main() from running.
            resources.enter_context(patch('ubuntu_image.__main__.main'))
            code = main(('--debug', 'model.assertion'))
        self.assertEqual(code, 0)
        mock.assert_called_once_with(level=logging.DEBUG)

    def test_no_debug(self):
        with ExitStack() as resources:
            mock = resources.enter_context(
                patch('ubuntu_image.__main__.logging.basicConfig'))
            resources.enter_context(patch(
                'ubuntu_image.__main__.ModelAssertionBuilder',
                EarlyExitModelAssertionBuilder))
            # Prevent actual main() from running.
            resources.enter_context(patch('ubuntu_image.__main__.main'))
            code = main(('model.assertion',))
        self.assertEqual(code, 0)
        mock.assert_not_called()

    def test_state_machine_exception(self):
        with ExitStack() as resources:
            resources.enter_context(patch(
                'ubuntu_image.__main__.ModelAssertionBuilder',
                CrashingModelAssertionBuilder))
            mock = resources.enter_context(patch(
                'ubuntu_image.__main__._logger.exception'))
            code = main(('model.assertion',))
            self.assertEqual(code, 1)
            self.assertEqual(
                mock.call_args_list[-1], call('Crash in state machine'))

    def test_state_machine_snap_command_fails(self):
        # The `snap prepare-image` command fails and main exits with non-zero.
        #
        # This tests needs to run the actual snap() helper function, not
        # the testsuite-wide mock.  This is appropriate since we're
        # mocking it ourselves here.
        if NosePlugin.snap_mocker is not None:
            NosePlugin.snap_mocker.patcher.stop()
            self._resources.callback(NosePlugin.snap_mocker.patcher.start)
        self._resources.enter_context(patch(
            'ubuntu_image.helpers.subprocess_run',
            return_value=SimpleNamespace(
                returncode=1,
                stdout='command stdout',
                stderr='command stderr',
                check_returncode=check_returncode,
                )))
        self._resources.enter_context(LogCapture())
        self._resources.enter_context(patch(
            'ubuntu_image.__main__.ModelAssertionBuilder',
            XXXModelAssertionBuilder))
        workdir = self._resources.enter_context(TemporaryDirectory())
        imgfile = os.path.join(workdir, 'my-disk.img')
        code = main(('--until', 'prepare_filesystems',
                     '--channel', 'edge',
                     '--workdir', workdir,
                     '--output', imgfile,
                     'model.assertion'))
        self.assertEqual(code, 1)


class TestMainWithModel(TestCase):
    def setUp(self):
        super().setUp()
        self._resources = ExitStack()
        self.addCleanup(self._resources.close)
        # Capture builtin print() output.
        self._stdout = StringIO()
        self._stderr = StringIO()
        self._resources.enter_context(
            patch('argparse._sys.stdout', self._stdout))
        # Capture stderr since this is where argparse will spew to.
        self._resources.enter_context(
            patch('argparse._sys.stderr', self._stderr))
        # Set up a few other useful things for these tests.
        self._resources.enter_context(
            patch('ubuntu_image.__main__.logging.basicConfig'))
        self.model_assertion = resource_filename(
            'ubuntu_image.tests.data', 'model.assertion')

    def test_output(self):
        self._resources.enter_context(patch(
            'ubuntu_image.__main__.ModelAssertionBuilder',
            DoNothingBuilder))
        tmpdir = self._resources.enter_context(TemporaryDirectory())
        imgfile = os.path.join(tmpdir, 'my-disk.img')
        self.assertFalse(os.path.exists(imgfile))
        main(('--output', imgfile, self.model_assertion))
        self.assertTrue(os.path.exists(imgfile))

    def test_output_to_snaps_tmp(self):
        # LP: 1646968 - Snappy maps /tmp to a private directory so when run as
        # a snap you cannot use `-o /tmp/something`.
        #
        # For reference see:
        # http://snapcraft.io/docs/reference/env
        # http://www.zygoon.pl/2016/08/snap-execution-environment.html
        self._resources.enter_context(envar('SNAP_NAME', 'crackle-pop'))
        self._resources.enter_context(patch(
            'ubuntu_image.__main__.ModelAssertionBuilder',
            DoNothingBuilder))
        tmpdir = self._resources.enter_context(TemporaryDirectory())
        imgfile = os.path.join(tmpdir, 'my-disk.img')
        self.assertFalse(os.path.exists(imgfile))
        code = main(('--output', '/tmp/my.img', self.model_assertion))
        self.assertEqual(code, 1)
        self.assertEqual(
            self._stderr.getvalue(),
            'ubuntu-image snap cannot write images to /tmp\n')

    def test_no_output_as_snaps_pwd_tmp(self):
        # LP: 1646968 - With no --output, if the current working directory is
        # /tmp, refuse to run.
        #
        # For reference see:
        # http://snapcraft.io/docs/reference/env
        # http://www.zygoon.pl/2016/08/snap-execution-environment.html
        self._resources.enter_context(envar('SNAP_NAME', 'crackle-pop'))
        # XXX This could fail if /tmp doesn't exist for some reason.  It might
        # be better to actually mock os.getcwd() to return /tmp instead.  I'm
        # not doing that here though because I don't want to track down the
        # mock location.
        self._resources.enter_context(chdir('/tmp'))
        self._resources.enter_context(patch(
            'ubuntu_image.__main__.ModelAssertionBuilder',
            DoNothingBuilder))
        tmpdir = self._resources.enter_context(TemporaryDirectory())
        imgfile = os.path.join(tmpdir, 'my-disk.img')
        self.assertFalse(os.path.exists(imgfile))
        code = main((self.model_assertion,))
        self.assertEqual(code, 1)
        self.assertEqual(
            self._stderr.getvalue(),
            'ubuntu-image snap cannot write images to /tmp\n')

    def test_resume_and_model_assertion(self):
        with self.assertRaises(SystemExit) as cm:
            main(('--resume', self.model_assertion))
        self.assertEqual(cm.exception.code, 2)

    def test_no_resume_and_no_model_assertion(self):
        with self.assertRaises(SystemExit) as cm:
            main(('--until', 'whatever'))
        self.assertEqual(cm.exception.code, 2)

    def test_resume_without_workdir(self):
        with self.assertRaises(SystemExit) as cm:
            main(('--resume',))
        self.assertEqual(cm.exception.code, 2)

    @skipIf('UBUNTU_IMAGE_TESTS_NO_NETWORK' in os.environ,
            'Cannot run this test without network access')
    def test_save_resume(self):
        self._resources.enter_context(patch(
            'ubuntu_image.__main__.ModelAssertionBuilder',
            XXXModelAssertionBuilder))
        workdir = self._resources.enter_context(TemporaryDirectory())
        imgfile = os.path.join(workdir, 'my-disk.img')
        main(('--until', 'prepare_filesystems',
              '--channel', 'edge',
              '--workdir', workdir,
              '--output', imgfile,
              self.model_assertion))
        self.assertTrue(os.path.exists(os.path.join(
            workdir, '.ubuntu-image.pck')))
        self.assertFalse(os.path.exists(imgfile))
        main(('--resume', '--workdir', workdir))
        self.assertTrue(os.path.exists(imgfile))

    def test_until(self):
        workdir = self._resources.enter_context(TemporaryDirectory())
        self._resources.enter_context(patch(
            'ubuntu_image.__main__.ModelAssertionBuilder',
            DoNothingBuilder))
        main(('--until', 'populate_rootfs_contents',
              '--channel', 'edge',
              '--workdir', workdir,
              self.model_assertion))
        # The pickle file will tell us how far the state machine got.
        with open(os.path.join(workdir, '.ubuntu-image.pck'), 'rb') as fp:
            pickle_state = load(fp).__getstate__()
        # This is the *next* state to execute.
        self.assertEqual(pickle_state['state'], ['populate_rootfs_contents'])

    def test_thru(self):
        workdir = self._resources.enter_context(TemporaryDirectory())
        self._resources.enter_context(patch(
            'ubuntu_image.__main__.ModelAssertionBuilder',
            DoNothingBuilder))
        main(('--thru', 'populate_rootfs_contents',
              '--channel', 'edge',
              '--workdir', workdir,
              self.model_assertion))
        # The pickle file will tell us how far the state machine got.
        with open(os.path.join(workdir, '.ubuntu-image.pck'), 'rb') as fp:
            pickle_state = load(fp).__getstate__()
        # This is the *next* state to execute.
        self.assertEqual(pickle_state['state'], ['calculate_rootfs_size'])

    def test_resume_loads_pickle(self):
        workdir = self._resources.enter_context(TemporaryDirectory())
        self._resources.enter_context(patch(
            'ubuntu_image.__main__.ModelAssertionBuilder',
            EarlyExitLeaveATraceAssertionBuilder))
        main(('--until', 'prepare_image',
              '--workdir', workdir,
              self.model_assertion))
        self.assertFalse(os.path.exists(os.path.join(workdir, 'success')))
        main(('--workdir', workdir, '--resume'))
        self.assertTrue(os.path.exists(os.path.join(workdir, 'success')))


class TestMainWithBadGadget(TestCase):
    def setUp(self):
        super().setUp()
        self._resources = ExitStack()
        self.addCleanup(self._resources.close)
        self.model_assertion = resource_filename(
            'ubuntu_image.tests.data', 'model.assertion')

    @skipIf('UBUNTU_IMAGE_TESTS_NO_NETWORK' in os.environ,
            'Cannot run this test without network access')
    def test_bad_gadget_log(self):
        log = self._resources.enter_context(LogCapture())
        workdir = self._resources.enter_context(TemporaryDirectory())
        self._resources.enter_context(patch(
            'ubuntu_image.__main__.ModelAssertionBuilder',
            BadGadgetModelAssertionBuilder))
        main(('--channel', 'edge',
              '--workdir', workdir,
              self.model_assertion))
        self.assertEqual(log.logs, [
            (logging.ERROR, 'gadget.yaml parse error: '
                            'GUID structure type with non-GPT schema'),
            (logging.ERROR, 'Use --debug for more information')
            ])

    @skipIf('UBUNTU_IMAGE_TESTS_NO_NETWORK' in os.environ,
            'Cannot run this test without network access')
    def test_bad_gadget_debug_log(self):
        log = self._resources.enter_context(LogCapture())
        workdir = self._resources.enter_context(TemporaryDirectory())
        self._resources.enter_context(patch(
            'ubuntu_image.__main__.ModelAssertionBuilder',
            BadGadgetModelAssertionBuilder))
        main(('--channel', 'edge',
              '--debug',
              '--workdir', workdir,
              self.model_assertion))
        self.assertEqual(log.logs, [
            (logging.ERROR, 'uncaught exception in state machine step: '
                            '[2] load_gadget_yaml'),
            'IMAGINE THE TRACEBACK HERE',
            (logging.ERROR, 'gadget.yaml parse error'),
            'IMAGINE THE TRACEBACK HERE',
            ])
