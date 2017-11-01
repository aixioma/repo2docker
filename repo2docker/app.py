"""repo2docker: convert git repositories into jupyter-suitable docker images

Images produced by repo2docker can be used with Jupyter notebooks standalone
or with BinderHub.

Usage:

    python -m repo2docker https://github.com/you/your-repo
"""
import sys
import json
import os
import time
import logging
import uuid
import shutil
import argparse
from pythonjsonlogger import jsonlogger
import escapism


from traitlets.config import Application, LoggingConfigurable
from traitlets import Type, Bool, Unicode, Dict, List, default, Tuple
import docker
from docker.utils import kwargs_from_env

import subprocess

from .detectors import (
    BuildPack, PythonBuildPack, DockerBuildPack, LegacyBinderDockerBuildPack,
    CondaBuildPack, JuliaBuildPack, Python2BuildPack, BaseImage
)
from .utils import execute_cmd
from . import __version__


def compose(buildpacks, parent=None):
    """
    Shortcut to compose many buildpacks together
    """
    image = buildpacks[0](parent=parent)
    for buildpack in buildpacks[1:]:
        image = image.compose_with(buildpack(parent=parent))
    return image


class Repo2Docker(Application):
    name = 'jupyter-repo2docker'
    version = __version__
    description = __doc__

    @default('log_level')
    def _default_log_level(self):
        return logging.INFO

    git_workdir = Unicode(
        "/tmp",
        config=True,
        help="""
        The directory to use to check out git repositories into.

        Should be somewhere ephemeral, such as /tmp
        """
    )

    buildpacks = List(
        [
            (LegacyBinderDockerBuildPack, ),
            (DockerBuildPack, ),

            (BaseImage, CondaBuildPack, JuliaBuildPack),
            (BaseImage, CondaBuildPack),

            (BaseImage, PythonBuildPack, Python2BuildPack, JuliaBuildPack),
            (BaseImage, PythonBuildPack, JuliaBuildPack),
            (BaseImage, PythonBuildPack, Python2BuildPack),
            (BaseImage, PythonBuildPack),
        ],
        config=True,
        help="""
        Ordered list of BuildPacks to try to use to build a git repository.
        """
    )

    default_buildpack = Tuple(
        (BaseImage, PythonBuildPack),
        config=True,
        help="""
        The build pack to use when no buildpacks are found
        """
    )

    def fetch(self, url, ref, checkout_path):
        def _clone(depth=None):
            if depth is not None:
                command = ['git', 'clone', '--depth', str(depth),
                           url, checkout_path]
            else:
                command = ['git', 'clone', url, checkout_path]

            try:
                for line in execute_cmd(command, capture=self.json_logs):
                    self.log.info(line, extra=dict(phase='fetching'))
            except subprocess.CalledProcessError:
                self.log.error('Failed to clone repository!',
                               extra=dict(phase='failed'))
                raise RuntimeError("Failed to clone %s." % url)

        def _unshallow():
            try:
                for line in execute_cmd(['git', 'fetch', '--unshallow'],
                                        capture=self.json_logs,
                                        cwd=checkout_path):
                    self.log.info(line, extra=dict(phase='fetching'))
            except subprocess.CalledProcessError:
                self.log.error('Failed to unshallow repository!',
                               extra=dict(phase='failed'))
                raise RuntimeError("Failed to create a full clone of"
                                   " %s." % url)

        def _contains(ref):
            try:
                for line in execute_cmd(['git', 'cat-file', '-t', ref],
                                        capture=self.json_logs,
                                        cwd=checkout_path):
                    self.log.debug(line, extra=dict(phase='fetching'))
            except subprocess.CalledProcessError:
                return False

            return True

        def _checkout(ref):
            try:
                for line in execute_cmd(['git', 'reset', '--hard', ref],
                                        cwd=checkout_path,
                                        capture=self.json_logs):
                    self.log.info(line, extra=dict(phase='fetching'))
            except subprocess.CalledProcessError:
                self.log.error('Failed to check out ref %s', ref,
                               extra=dict(phase='failed'))
                raise RuntimeError("Failed to checkout reference %s for"
                                   " %s." % (ref, url))

        # create a shallow clone first
        _clone(depth=50)

        if not _contains(ref):
            # have to create a full clone
            _unshallow()
        _checkout(ref)

    def get_argparser(self):
        argparser = argparse.ArgumentParser()
        argparser.add_argument(
            '--config',
            default='repo2docker_config.py',
            help="Path to config file for repo2docker"
        )

        argparser.add_argument(
            '--json-logs',
            default=False,
            action='store_true',
            help='Emit JSON logs instead of human readable logs'
        )

        argparser.add_argument(
            'repo',
            help='Path to repository that should be built. Could be local path or a git URL.'
        )

        argparser.add_argument(
            '--image-name',
            help='Name of image to be built. If unspecified will be autogenerated'
        )

        argparser.add_argument(
            '--ref',
            help='If building a git url, which ref to check out'
        )

        argparser.add_argument(
            '--debug',
            help="Turn on debug logging",
            action='store_true',
        )

        argparser.add_argument(
            '--no-build',
            dest='build',
            action='store_false',
            help="Do not actually build the image. Useful in conjunction with --debug."
        )

        argparser.add_argument(
            'cmd',
            nargs=argparse.REMAINDER,
            help='Custom command to run after building container'
        )

        argparser.add_argument(
            '--no-run',
            dest='run',
            action='store_false',
            help='Do not run container after it has been built'
        )

        argparser.add_argument(
            '--no-clean',
            dest='clean',
            action='store_false',
            help="Don't clean up remote checkouts after we are done"
        )

        argparser.add_argument(
            '--push',
            dest='push',
            action='store_true',
            help='Push docker image to repository'
        )

        return argparser

    def json_excepthook(self, etype, evalue, traceback):
        """Called on an uncaught exception when using json logging

        Avoids non-JSON output on errors when using --json-logs
        """
        self.log.error("Error during build: %s", evalue,
            exc_info=(etype, evalue, traceback),
            extra=dict(phase='failed'),
        )


    def initialize(self):
        args = self.get_argparser().parse_args()

        if args.debug:
            self.log_level = logging.DEBUG

        self.load_config_file(args.config)

        if os.path.exists(args.repo):
            # Let's treat this as a local directory we are building
            self.repo_type = 'local'
            self.repo = args.repo
            self.ref = None
            self.cleanup_checkout = False
        else:
            self.repo_type = 'remote'
            self.repo = args.repo
            self.ref = args.ref
            self.cleanup_checkout = args.clean

        if args.json_logs:
            # register JSON excepthook to avoid non-JSON output on errors
            sys.excepthook = self.json_excepthook
            # Need to reset existing handlers, or we repeat messages
            logHandler = logging.StreamHandler()
            formatter = jsonlogger.JsonFormatter()
            logHandler.setFormatter(formatter)
            self.log.handlers = []
            self.log.addHandler(logHandler)
            self.log.setLevel(logging.INFO)
        else:
            # due to json logger stuff above,
            # our log messages include carriage returns, newlines, etc.
            # remove the additional newline from the stream handler
            self.log.handlers[0].terminator = ''
            # We don't want a [Repo2Docker] on all messages
            self.log.handlers[0].formatter = logging.Formatter(fmt='%(message)s')

        if args.image_name:
            self.output_image_spec = args.image_name
        else:
            # Attempt to set a sane default!
            # HACK: Provide something more descriptive?
            self.output_image_spec = 'r2d' + escapism.escape(self.repo, escape_char='-').lower() + str(int(time.time()))

        self.push = args.push
        self.run = args.run
        self.json_logs = args.json_logs

        self.build = args.build
        if not self.build:
            # Can't push nor run if we aren't building
            self.run = False
            self.push = False

        self.run_cmd = args.cmd


    def push_image(self):
        client = docker.APIClient(version='auto', **kwargs_from_env())
        # Build a progress setup for each layer, and only emit per-layer info every 1.5s
        layers = {}
        last_emit_time = time.time()
        for line in client.push(self.output_image_spec, stream=True):
            progress = json.loads(line.decode('utf-8'))
            if 'error' in progress:
                self.log.error(progress['error'], extra=dict(phase='failed'))
                sys.exit(1)
            if 'id' not in progress:
                continue
            if 'progressDetail' in progress and progress['progressDetail']:
                layers[progress['id']] = progress['progressDetail']
            else:
                layers[progress['id']] = progress['status']
            if time.time() - last_emit_time > 1.5:
                self.log.info('Pushing image\n', extra=dict(progress=layers, phase='pushing'))
                last_emit_time = time.time()

    def run_image(self):
        client = docker.from_env(version='auto')
        port = self._get_free_port()
        if not self.run_cmd:
            port = str(self._get_free_port())
            run_cmd = ['jupyter', 'notebook', '--ip', '0.0.0.0', '--port', port]
            ports={'%s/tcp' % port: port}
        else:
            run_cmd = self.run_cmd
            ports = {}
        container = client.containers.run(
            self.output_image_spec,
            ports=ports,
            detach=True,
            command=run_cmd
        )
        while container.status == 'created':
            time.sleep(0.5)
            container.reload()

        try:
            for line in container.logs(stream=True):
                self.log.info(line.decode('utf-8'), extra=dict(phase='running'))
        finally:
            container.reload()
            if container.status == 'running':
                self.log.info('Stopping container...\n', extra=dict(phase='running'))
                container.kill()
            exit_code = container.attrs['State']['ExitCode']
            container.remove()
            sys.exit(exit_code)

    def _get_free_port(self):
        """
        Hacky method to get a free random port on local host
        """
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("",0))
        port = s.getsockname()[1]
        s.close()
        return port

    def start(self):
        if self.repo_type == 'local':
            checkout_path = self.repo
        else:
            checkout_path = os.path.join(self.git_workdir, str(uuid.uuid4()))
            self.fetch(
                self.repo,
                self.ref,
                checkout_path
            )

        os.chdir(checkout_path)
        picked_buildpack = compose(self.default_buildpack, parent=self)

        for bp_spec in self.buildpacks:
            bp = compose(bp_spec, parent=self)
            if bp.detect():
                picked_buildpack = bp
                break

        self.log.debug(picked_buildpack.render(), extra=dict(phase='building'))

        if self.build:
            self.log.info('Using %s builder\n', bp.name, extra=dict(phase='building'))
            for l in picked_buildpack.build(self.output_image_spec):
                if 'stream' in l:
                    self.log.info(l['stream'], extra=dict(phase='building'))
                elif 'error' in l:
                    self.log.info(l['error'], extra=dict(phase='failure'))
                    sys.exit(1)
                elif 'status' in l:
                        self.log.info('Fetching base image...\r', extra=dict(phase='building'))
                else:
                    self.log.info(json.dumps(l), extra=dict(phase='building'))

        if self.cleanup_checkout:
            shutil.rmtree(checkout_path, ignore_errors=True)

        if self.push:
            self.push_image()

        if self.run:
            self.run_image()
