#!/usr/bin/env python

##############################################################################
#
# pkgbuild.py
# version 1.0, by John Wiegley
#
# TODO: [MED]  Generate service manifests, if just an /etc/init.d script
# TODO: [EASY] Allow naming the service (like 'network/dovecot:imap')
#
# This script turns standardish tarballs directly into Solaris packages.  For
# most packages, simply do this:
#
#   pkgbuild.py foo-1.0.tar.gz
#
# At the moment, I depend on a few things:
#
#   2. The tarball is named NAME-VERSION.tar.EXT.
#   3. The tarball expands to a directory named NAME-VERSION.
#
# Exceptions can be made by subclassing 'Package'.  There are several examples
# of how to do this toward the end of this file.  Look for "CUSTOM".
#
# Also: If, for any package, an SMF manifest file named NAME.xml exists in the
# same directory as the tarball, it will be imported as a service during the
# package's installation scripts (and also properly removed during uninstall).
#
##############################################################################

import inspect
import logging
import logging.handlers
import optparse
import os
import re
import shutil
import stat
import subprocess
import sys
import time

from os.path import *

TMPDIR   = '/tmp'
INSTALL  = '/usr/bin/ginstall'
archname = 'i386-solaris2.11'

os.environ['PATH'] = '/usr/gnu/bin:' + os.environ['PATH']

make_opts = ('-j8',)

##############################################################################

LEVELS = {'DEBUG':    logging.DEBUG,
          'INFO':     logging.INFO,
          'WARNING':  logging.WARNING,
          'ERROR':    logging.ERROR,
          'CRITICAL': logging.CRITICAL }

class CommandLineApp(object):
    "Base class for building command line applications."

    force_exit  = True           # If true, always ends run() with sys.exit()
    log_handler = None

    options = {
        'verbose':  False,
        'logfile':  False,
        'loglevel': False
        }

    def __init__(self):
        "Initialize CommandLineApp."
        # Create the logger
        self.log = logging.getLogger(os.path.basename(sys.argv[0]))
        ch = logging.StreamHandler()
        formatter = logging.Formatter("%(name)s: %(levelname)s: %(message)s")
        ch.setFormatter(formatter)
        self.log.addHandler(ch)
        self.log_handler = ch

        # Setup the options parser
        usage = 'usage: %prog [options] <BOUND-IP-ADDRESS>'
        op = self.option_parser = optparse.OptionParser(usage = usage)

        op.add_option('-v', '--verbose',
                      action='store_true', dest='verbose',
                      default=False, help='show informational messages')
        op.add_option('-q', '--quiet',
                      action='store_true', dest='quiet',
                      default=False, help='do not show log messages on console')
        op.add_option('', '--log', metavar='FILE',
                      type='string', action='store', dest='logfile',
                      default=False, help='append logging data to FILE')
        op.add_option('', '--loglevel', metavar='LEVEL',
                      type='string', action='store', dest='loglevel',
                      default=False, help='set log level: DEBUG, INFO, WARNING, ERROR, CRITICAL')

    def main(self, *args):
        """Main body of your application.

        This is the main portion of the app, and is run after all of the
        arguments are processed.  Override this method to implment the primary
        processing section of your application."""
        pass

    def handleInterrupt(self):
        """Called when the program is interrupted via Control-C or SIGINT.
        Returns exit code."""
        self.log.error('Canceled by user.')
        return 1

    def handleMainException(self):
        "Invoked when there is an error in the main() method."
        if not self.options.verbose:
            self.log.exception('Caught exception')
        return 1

    ## INTERNALS (Subclasses should not need to override these methods)

    def run(self):
        """Entry point.

        Process options and execute callback functions as needed.  This method
        should not need to be overridden, if the main() method is defined."""
        # Process the options supported and given
        self.options, main_args = self.option_parser.parse_args()

        if self.options.logfile:
            fh = logging.handlers.RotatingFileHandler(self.options.logfile,
                                                      maxBytes = (1024 * 1024),
                                                      backupCount = 5)
            formatter = logging.Formatter("%(asctime)s - %(levelname)s: %(message)s")
            fh.setFormatter(formatter)
            self.log.addHandler(fh)

        if self.options.quiet:
            self.log.removeHandler(self.log_handler)
            ch = logging.handlers.SysLogHandler()
            formatter = logging.Formatter("%(name)s: %(levelname)s: %(message)s")
            ch.setFormatter(formatter)
            self.log.addHandler(ch)
            self.log_handler = ch

        if self.options.loglevel:
            self.log.setLevel(LEVELS[self.options.loglevel])
        elif self.options.verbose:
            self.log.setLevel(logging.DEBUG)
        else:
            self.log.setLevel(logging.INFO)

        exit_code = 0
        try:
            # We could just call main() and catch a TypeError, but that would
            # not let us differentiate between application errors and a case
            # where the user has not passed us enough arguments.  So, we check
            # the argument count ourself.
            argspec = inspect.getargspec(self.main)
            expected_arg_count = len(argspec[0]) - 1

            if len(main_args) >= expected_arg_count:
                exit_code = self.main(*main_args)
            else:
                self.log.debug('Incorrect argument count (expected %d, got %d)' %
                               (expected_arg_count, len(main_args)))
                self.option_parser.print_help()
                exit_code = 1

        except KeyboardInterrupt:
            exit_code = self.handleInterrupt()

        except SystemExit, msg:
            exit_code = msg.args[0]

        except Exception:
            exit_code = self.handleMainException()
            if self.options.verbose:
                raise

        if self.force_exit:
            sys.exit(exit_code)
        return exit_code

    def mkreader(self, *args, **kwargs):
        self.log.info(str(args))
        kwargs['stdout'] = subprocess.PIPE
        p = subprocess.Popen(args, **kwargs)
        return p.stdout

    def mkwriter(self, *args, **kwargs):
        self.log.info(str(args))
        kwargs['stdin'] = subprocess.PIPE
        p = subprocess.Popen(args, **kwargs)
        return p.stdin

    def shuttle(self, reader, writer):
        data = reader.read(8192)
        while data:
            writer.write(data)
            data = reader.read(8192)

    def shell(self, *args, **kwargs):
        if 'stdout' not in kwargs: kwargs['stdout'] = sys.stdout
        if 'stderr' not in kwargs: kwargs['stderr'] = sys.stderr

        self.log.info(str(args))
        if subprocess.call(args, **kwargs) == 0:
            return True
        else:
            raise Exception("Command failed: " + str(args))

##############################################################################

class ServiceManifest(object):
    name            = None
    title           = None
    service_name    = None
    dependencies    = [ 'filesystem' ]
    config_files    = []
    init_script     = None
    start_command   = None
    stop_command    = None
    refresh_command = None
    restart_command = None

    def __init__(self, name, title, service_name,
                 dependencies = [ 'filesystem' ], config_files = [],
                 init_script = None):
        self.name         = name
        self.title        = title
        self.service_name = service_name
        self.dependencies = dependencies
        self.config_files = config_files
        self.init_script  = init_script

    def add_dependency(self, dependency):
        self.dependencies.append(dependency)

    def add_config_file(self, config_file):
        self.config_files.append(config_file)

    def set_init_script(self, init_script):
        self.init_script = init_script

    def set_start_command(self, command, timeout=60):
        self.start_command = (command, timeout)
    def set_stop_command(self, command, timeout=60):
        self.stop_command = (command, timeout)
    def set_refresh_command(self, command, timeout=60):
        self.refresh_command = (command, timeout)
    def set_restart_command(self, command, timeout=60):
        self.restart_command = (command, timeout)

    network_dependency = """
    <dependency name="network"
                grouping="require_all"
                restart_on="error"
                type="service">
      <service_fmri value="svc:/milestone/network:default" />
    </dependency>
"""

    filesystem_dependency = """
    <dependency name="filesystem-local"
                grouping="require_all"
                restart_on="none"
                type="service">
      <service_fmri value="svc:/system/filesystem/local:default" />
    </dependency>
"""

    config_dependency = """
    <dependency name="%(name)s-config-file"
                grouping="require_all"
                restart_on="none"
                type="path">
      <service_fmri value="file://%(path)s" />
    </dependency>
"""

    method_template = """
    <exec_method name="%(name)s"
                 type="method"
                 exec="%(command)s"
                 timeout_seconds="%(timeout)d">
    </exec_method>
"""

    manifest_template = """<?xml version="1.0"?>
<!DOCTYPE service_bundle SYSTEM "/usr/share/lib/xml/dtd/service_bundle.dtd.1">
<service_bundle type="manifest" name="%(name)s">
  <service name="%(service_name)s" type="service" version="1">
    <create_default_instance enabled="true"/>
    %(dependencies)s
    <template>
      <common_name>
        <loctext xml:lang="C">%(title)s</loctext>
      </common_name>
    </template>
  </service>
</service_bundle>
"""

    def generate_manifest(self, path):
        manifest_details = {}

        dependencies_xml = ""
        for dependency in self.dependencies:
            if dependency == 'network':
                dependencies_xml += self.network_dependency
            elif dependency == 'filesystem':
                dependencies_xml += self.filesystem_dependency

        for config in self.config_files:
            dependencies_xml +=  self.config_dependency % \
                { 'name': re.sub('\.', '-', basename(config)),
                  'path': config }

        if self.start_command and self.stop_command:
            dependencies_xml += \
                self.method_template % { 'name':    'start',
                                         'command': self.start_command[0],
                                         'timeout': self.start_command[1] }
            dependencies_xml += \
                self.method_template % { 'name':    'stop',
                                         'command': self.stop_command[0],
                                         'timeout': self.stop_command[1] }

            if self.refresh_command:
                dependencies_xml += \
                    self.method_template % { 'name':    'refresh',
                                             'command': self.refresh_command[0],
                                             'timeout': self.refresh_command[1] }
            else:
                dependencies_xml += \
                    self.method_template % { 'name':    'refresh',
                                             'command': self.start_command[0] + \
                                                 '; ' + self.stop_command[0],
                                             'timeout': self.start_command[1] + \
                                                 self.stop_command[1] }
            if self.restart_command:
                dependencies_xml += \
                    self.method_template % { 'name':    'restart',
                                             'command': self.restart_command[0],
                                             'timeout': self.restart_command[1] }
            else:
                dependencies_xml += \
                    self.method_template % { 'name':    'restart',
                                             'command': self.start_command[0] + \
                                                 '; ' + self.stop_command[0],
                                             'timeout': self.start_command[1] + \
                                                 self.stop_command[1]}
        elif self.init_script:
            dependencies_xml += \
                self.method_template % { 'name':    'start',
                                         'command': '%s start' % self.init_script,
                                         'timeout': 60 }
            dependencies_xml += \
                self.method_template % { 'name':    'stop',
                                         'command': '%s stop' % self.init_script,
                                         'timeout': 60 }
            dependencies_xml += \
                self.method_template % { 'name':    'refresh',
                                         'command': '%s stop; %s start' % \
                                         (self.init_script, self.init_script),
                                         'timeout': 60 }
            dependencies_xml += \
                self.method_template % { 'name':    'restart',
                                         'command': '%s stop; %s start' % \
                                         (self.init_script, self.init_script),
                                         'timeout': 60 }

        manifest_details['name']         = self.name
        manifest_details['title']        = self.title
        manifest_details['service_name'] = self.service_name
        manifest_details['dependencies'] = dependencies_xml

        with open(path, 'w') as fd:
            fd.write(self.manifest_template % manifest_details)

    def manifest_path(self):
        path = join(TMPDIR, '%s.xml' % self.name)
        self.generate_manifest(path)
        return path

class PrototypeFile(object):
    fd             = None
    preinstall_fd  = None
    postinstall_fd = None
    preremove_fd   = None
    postremove_fd  = None

    def __init__(self):
        self.fd = open('prototype', 'w')

    def __del__(self):
        self.close()

    def write(self, data):
        self.fd.write(data)

    def close(self):
        if self.preinstall_fd:
            self.preinstall_fd.close()
            os.chmod('preinstall', 0755)
        if self.postinstall_fd:
            self.postinstall_fd.close()
            os.chmod('postinstall', 0755)
        if self.preremove_fd:
            self.preremove_fd.close()
            os.chmod('preremove', 0755)
        if self.postremove_fd:
            self.postremove_fd.close()
            os.chmod('postremove', 0755)
        self.fd.close()

    def preinstall(self, line):
        if not self.preinstall_fd:
            self.preinstall_fd = open('preinstall', 'w')
            self.preinstall_fd.write('#!/bin/sh\n')
            self.include('preinstall')
        self.preinstall_fd.write(line)
        self.preinstall_fd.write('\n')

    def postinstall(self, line):
        if not self.postinstall_fd:
            self.postinstall_fd = open('postinstall', 'w')
            self.postinstall_fd.write('#!/bin/sh\n')
            self.include('postinstall')
        self.postinstall_fd.write(line)
        self.postinstall_fd.write('\n')

    def preremove(self, line):
        if not self.preremove_fd:
            self.preremove_fd = open('preremove', 'w')
            self.preremove_fd.write('#!/bin/sh\n')
            self.include('preremove')
        self.preremove_fd.write(line)
        self.preremove_fd.write('\n')

    def postremove(self, line):
        if not self.postremove_fd:
            self.postremove_fd = open('postremove', 'w')
            self.postremove_fd.write('#!/bin/sh\n')
            self.include('postremove')
        self.postremove_fd.write(line)
        self.postremove_fd.write('\n')

    def include(self, name, path=None):
        if path:
            self.fd.write('i %s=%s\n' % (name, path))
        else:
            self.fd.write('i %s\n' % name)

class PkgInfoFile(object):
    name     = None
    title    = None
    version  = None
    category = None

    def __init__(self, name, title, version, category='application'):
        self.name     = name
        self.title    = title
        self.version  = version
        self.category = category

    def close(self):
        with open('pkginfo', 'w') as fd:
            fd.write('''PKG=%s
NAME=%s
VERSION=%s
CATEGORY=%s
''' % (self.name, self.title, self.version, self.category))

class Package(object):
    app      = None
    tarball  = ""
    base     = ""
    name     = ""
    title    = ""
    version  = ""
    manifest = None

    def __init__(self, app, tarball=None):
        self.app = app

        if tarball:
            match = re.match('(([^0-9]+?)-([-0-9_.pba]+))\.tar\.(gz|bz2|xz)+$', tarball)
            if not match:
                raise Exception("Cannot parse tarball name: " + tarball)

            self.tarball = match.group(0)
            self.base    = match.group(1)
            self.name    = match.group(2)
            if not self.title:
                self.title = self.name
            self.version = match.group(3)

        app.log.info("Tarball = %s" % self.tarball)
        app.log.info("Base    = %s" % self.base)
        app.log.info("Name    = %s" % self.name)
        app.log.info("Title   = %s" % self.title)
        app.log.info("Version = %s" % self.version)

        path = join(os.getcwd(), '%s.xml' % self.name)
        if isfile(path):
            self.manifest = path

    def maybe_call(self, name, *args, **kwargs):
        try: method = getattr(self, name)
        except AttributeError: pass
        else: method(*args, **kwargs)

    def clean(self):
        if isdir(self.base):
            shutil.rmtree(self.base)

    def unpack(self):
        assert isfile(self.tarball)

        if '.xz' in self.tarball:
            if not isfile('/usr/bin/xz'):
                raise Exception('Please install the xz package')

            self.app.shuttle(self.app.mkreader("xz", "-dc", self.tarball),
                             self.app.mkwriter("tar", "-xf", "-"))

        elif '.bz2' in self.tarball:
            if not isfile('/usr/bin/bzip2'):
                raise Exception('Please install the bzip2 package')

            self.app.shuttle(self.app.mkreader("bzip2", "-dc", self.tarball),
                             self.app.mkwriter("tar", "-xf", "-"))

        elif '.gz' in self.tarball:
            if not isfile('/usr/bin/gzip'):
                raise Exception('Please install the gzip package')

            self.app.shuttle(self.app.mkreader("gzip", "-dc", self.tarball),
                             self.app.mkwriter("tar", "-xf", "-"))

        self.app.shell('chown', '-R', 'root:root', self.base)

    def prepare(self):
        self.app.shell('git', 'init')

        # Remove all hook files
        for entry in os.listdir('.git/hooks'):
            os.remove(join('.git/hooks', entry))

        self.app.shell('git', 'add', '.')
        self.app.shell('git', 'commit', '-q', '-m', 'Base')
        self.app.shell('git', 'gc', '--quiet')

    def configure(self):
        self.app.shell('./configure', '--prefix=/usr', '--sysconfdir=/etc')

    def build(self):
        opts = ['make',] + list(make_opts)
        self.app.shell(*opts)

    def ignore_products(self):
        with self.app.mkreader('git', 'ls-files',
                               '--other', '--exclude-standard') as fd:
            with open('.gitignore', 'w') as ignore:
                for line in fd:
                    ignore.write('/' + line)

    def install(self, staging):
        self.app.shell('make', 'INSTALL=%s' % INSTALL, 'DESTDIR=%s' % staging,
                       'install')

    def package(self):
        staging = join(TMPDIR, 'pkg-staging')

        # Clear out the staging area, since we're going to start populating it

        if isdir(staging):
            shutil.rmtree(staging)
        os.makedirs(staging)

        prototype = PrototypeFile()

        if self.manifest:
            manifest_is_tmpfile = False
            if isinstance(self.manifest, ServiceManifest):
                self.manifest = self.manifest.manifest_path()
                manifest_is_tmpfile = True

            profile_dir = join(staging, 'etc/svc/profile')
            if not isdir(profile_dir):
                os.makedirs(profile_dir)
            shutil.copy(self.manifest, profile_dir)
            if manifest_is_tmpfile:
                os.remove(self.manifest)

            prototype.postinstall('svccfg import /etc/svc/profile/%s' %
                                  basename(self.manifest))
            prototype.preremove('svcadm disable %s' % self.name)
            prototype.postremove('svccfg delete %s' % self.name)

        # Install the software into the staging area.  When the software was
        # configured, make sure that --prefix=/usr (or something reasonable),
        # and set --sysconfdir if necessary (if you want to make sure config
        # files go into /etc).

        self.install(staging)

        if not self.manifest and isfile(join(staging, 'etc/init.d', self.base)):
            self.manifest = ServiceManifest(self.name, self.title,
                                            'application/%s' % self.base,
                                            [ 'filesystem', 'network' ],
                                            init_script='etc/init.d' % self.base)

        # For directory names, match owner, group, access flags and dates with
        # whatever the system currently has installed.

        for root, dirs, files in os.walk(staging):
            for entry in map(lambda x: join(root, x)[len(staging)+1:], dirs):
                path = join('/', entry)
                if isdir(path):
                    info = os.stat(path)
                    os.chown(join(staging, entry), info.st_uid, info.st_gid)
                    os.chmod(join(staging, entry), info.st_mode)

        # Create the prototype file, and remove the root directory entry
        # (which should never be removed when doing a pkgrm!).

        for line in self.app.mkreader('pkgproto', '%s=/' % staging):
            if not re.match('d none / ', line):
                prototype.write(line)

        prototype.include('pkginfo')
        self.maybe_call('extend_prototype', prototype, staging)
        prototype.close()
        self.maybe_call('edit_prototype', prototype, staging)
        prototype = None

        # Create the pkginfo description file.  It's spartan.

        pkginfo = PkgInfoFile(self.name, self.title, self.version)
        pkginfo.close()

        # Make the package, then move it to the current directory with a
        # versioned pathname.

        self.mkpkg()

        shutil.rmtree(staging)

    def mkpkg(self):
        self.app.shell('pkgmk', '-o')
        self.app.shell('pkgtrans', '-s', '/var/spool/pkg',
                       join(TMPDIR, '%s.pkg' % self.base), self.name)

    def main(self):
        save_stdout = sys.stdout
        save_stderr = sys.stderr
        cwd         = os.getcwd()
        log_path    = '%s.log' % self.name

        with open(log_path, 'w') as out:
            try:
                sys.stdout = out
                sys.stderr = out

                self.clean(); out.flush()
                self.unpack(); out.flush()

                os.chdir(self.base)

                if isfile('/usr/bin/git'):
                    self.prepare(); out.flush()

                self.configure(); out.flush()
                self.build(); out.flush()

                if isfile('/usr/bin/git'):
                    self.ignore_products(); out.flush()

                self.package(); out.flush()

            finally:
                sys.stdout = save_stdout
                sys.stderr = save_stderr
                os.chdir(cwd)

        path = join(TMPDIR, '%s.pkg' % self.base)
        if isfile(path):
            self.app.log.info('=' * 50)
            self.app.log.info('Package %s built successfully.' % basename(path))
            self.app.log.info('=' * 50)

            shutil.copy(path, cwd)
            os.remove(path)
            os.remove(log_path)
            self.clean()
        else:
            self.app.log.info('=' * 50)
            self.app.log.info('%s FAILED to package!' % self.base)
            self.app.log.info('Output written to: ' + log_path)
            self.app.log.info('Source and build:  ' + self.base)
            self.app.log.info('Temp installation: ' +
                              join(TMPDIR, 'pkg-staging'))
            self.app.log.info('=' * 50)

##############################################################################
## CUSTOM PACKAGES ###########################################################
##############################################################################

class Apcupsd(Package):
    def __init__(self, app, tarball):
        Package.__init__(self, app, tarball)

        self.manifest = \
            ServiceManifest(self.name, self.title,
                            'system/ups/apcupsd',
                            [ 'filesystem' ],
                            [ '/etc/opt/apcupsd/apcupsd.conf' ],
                            init_script='/etc/init.d/apcupsd')

    def configure(self):
        self.app.shell('./configure', '--prefix=/usr', '--sysconfdir=/etc',
                       '--enable-usb')

    def edit_prototype(self, prototype, staging):
        shutil.rmtree(join(staging, 'etc/rc0.d'))
        shutil.rmtree(join(staging, 'etc/rc1.d'))
        shutil.rmtree(join(staging, 'etc/rc2.d'))

        with open('prototype_tmp', 'w') as tmp:
            for line in open('prototype', 'r'):
                if not re.search('/etc/rc', line):
                    tmp.write(line)

        os.remove('prototype')
        os.rename('prototype_tmp', 'prototype')

##############################################################################

class BerkeleyDB(Package):
    def __init__(self, app, tarball):
        Package.__init__(self, app, tarball)

    def configure(self):
        os.chdir('build_unix')
        self.app.shell('../dist/configure',
                       '--prefix=/usr', '--sysconfdir=/etc')

##############################################################################

class CMake(Package):
    def __init__(self, app, tarball):
        Package.__init__(self, app, tarball)

    def configure(self):
        self.app.shell('./configure', '--prefix=/usr')

##############################################################################

class Netatalk(Package):
    def __init__(self, app, tarball):
        Package.__init__(self, app, tarball)

        self.manifest = \
            ServiceManifest(self.name, self.title,
                            'network/afp/netatalk',
                            [ 'filesystem', 'network' ],
                            [ '/etc/netatalk/netatalk.conf',
                              '/etc/netatalk/afpd.conf',
                              '/etc/netatalk/AppleVolumes.default' ],
                            init_script='/etc/init.d/netatalk')

##############################################################################

class Ngircd(Package):
    def __init__(self, app, tarball):
        self.title = 'ngIRCd: Next Generation IRC Daemon'

        Package.__init__(self, app, tarball)

        self.manifest = ServiceManifest(self.name, self.title,
                                        'network/irc/ngircd',
                                        [ 'filesystem', 'network' ],
                                        [ '/etc/ngircd.conf' ])

        self.manifest.set_start_command('/usr/sbin/ngircd')
        self.manifest.set_stop_command(':kill -TERM')

    def configure(self):
        self.app.shell('./configure', '--prefix=/usr', '--sysconfdir=/etc',
                       '--without-devpoll')

##############################################################################

class GooglePerftools(Package):
    def __init__(self, app):
        self.base    = 'google-perftools-1.7'
        self.name    = 'google-perftools'
        self.title   = 'Google Perftools'
        self.version = '1.7'

        Package.__init__(self, app)

        self.app.log.info("Base    = %s" % self.base)
        self.app.log.info("Name    = %s" % self.name)
        self.app.log.info("Version = %s" % self.version)

    def clean(self): pass
    def unpack(self): pass
    def prepare(self): pass
    def ignore_products(self): pass

class RubyEnterprise(Package):
    def __init__(self, app, tarball):
        Package.__init__(self, app, tarball)

    def configure(self):
        os.chdir('source')

        path = os.getcwd()
        try:
            os.chdir('distro')
            perf = GooglePerftools(self.app)
            perf.main()
        finally:
            os.chdir(path)

        if not isfile('/usr/lib/libtcmalloc.a'):
            self.app.log.error('Please install the google-perftools ' +
                               'that was just built and build ruby again')
            sys.exit(1)

        self.app.shell('./configure', '--prefix=/usr', '--sysconfdir=/etc',
                       '--enable-mbari-api', '--program-suffix=18',
                       'CFLAGS=-O3')

        with open('Makefile_tmp', 'w') as tmp:
            for line in open('Makefile', 'r'):
                match = re.match('LIBS = (.*)', line)
                if match:
                    tmp.write('LIBS = $(PRELIBS) ' + match.group(1) + '\n')
                else:
                    tmp.write(line)
        os.remove('Makefile')
        os.rename('Makefile_tmp', 'Makefile')

        try:
            with self.app.mkwriter('patch', '-p2') as patch:
                patch.write('''
--- a/source/signal.c
+++ b/source/signal.c
@@ -16,6 +16,7 @@
 #include "rubysig.h"
 #include "node.h"
 #include <signal.h>
+#include <ucontext.h>
 #include <stdio.h>

 #ifdef __BEOS__
@@ -673,7 +674,7 @@ dump_machine_state(uc)
             uc->uc_mcontext->__ss.__eip, uc->uc_mcontext->__ss.__cs,
             uc->uc_mcontext->__ss.__ds, uc->uc_mcontext->__ss.__es,
             uc->uc_mcontext->__ss.__fs, uc->uc_mcontext->__ss.__gs);
-#elif defined(__i386__)
+#elif 0 && defined(__i386__)
   sig_printf(dump32, uc->uc_mcontext.gregs[REG_EAX], uc->uc_mcontext.gregs[REG_EBX],
             uc->uc_mcontext.gregs[REG_ECX], uc->uc_mcontext.gregs[REG_EDX],
             uc->uc_mcontext.gregs[REG_EDI], uc->uc_mcontext.gregs[REG_ESI],
''')
        except:
            pass

    def build(self):
        self.app.shell('make', 'PRELIBS=-Wl,-rpath,/usr/lib -L/usr/lib ' +
                       '-ltcmalloc_minimal')

    def install(self, staging):
        Package.install(self, staging)

        basedir        = join(staging, 'usr/lib/ruby')
        libdir         = join(basedir, '1.8')
        extlibdir      = join(libdir, archname)
        site_libdir    = join(basedir, 'site_ruby/1.8')
        site_extlibdir = join(site_libdir, archname)

        env = os.environ.copy()
        env['RUBYLIB'] = \
            '%s:%s:%s:%s' % (libdir, extlibdir, site_libdir, site_extlibdir)

        cwd = os.getcwd()
        try:
            os.chdir('../rubygems')
            self.app.shell(join(staging, 'usr/bin/ruby18'), 'setup.rb',
                           '--no-ri', '--no-rdoc', env=env)

            with open('/tmp/gem18', 'w') as fd:
                fd.write('#!/usr/bin/ruby18\n')

                linnum = 0
                for line in open(join(staging, 'usr/bin/gem18'), 'r'):
                    if linnum > 0:
                        fd.write(line)
                    linnum += 1

            os.remove(join(staging, 'usr/bin/gem18'))
            shutil.copyfile('/tmp/gem18', join(staging, 'usr/bin/gem18'))
            os.chmod(join(staging, 'usr/bin/gem18'), 0755)
        finally:
            os.chdir(cwd)

##############################################################################

class Ruby(Package):
    def __init__(self, app, tarball):
        Package.__init__(self, app, tarball)

    def configure(self):
        Package.configure(self)

        with open('/tmp/config.h', 'w') as fd:
            for line in open('.ext/include/%s/ruby/config.h' % archname, 'r'):
                if 'HAVE_DL_ITERATE_PHDR' not in line:
                    fd.write(line)

        os.remove('.ext/include/%s/ruby/config.h' % archname)
        shutil.copyfile('/tmp/config.h',
                        '.ext/include/%s/ruby/config.h' % archname)

##############################################################################

class Augeas(Package):
    def __init__(self, app, tarball):
        Package.__init__(self, app, tarball)

    def configure(self):
        Package.configure(self)

        with self.app.mkwriter('patch', '-p1') as patch:
            patch.write('''
--- a/src/regexp.c
+++ b/src/regexp.c
@@ -50,7 +50,7 @@ char *regexp_escape(const struct regexp *r) {
     ret = fa_restrict_alphabet(r->pattern->str, strlen(r->pattern->str),
                                &nre, &nre_len, 2, 1);
     if (ret == 0) {
-        pat = escape(nre, nre_len);
+        pat = escape(nre, nre_len, RX_ESCAPES);
         free(nre);
     }
 #endif
''')

##############################################################################

class GVPE(Package):
    def __init__(self, app, tarball):
        self.title = 'GVPE: GNU Virtual Private Ethernet'

        Package.__init__(self, app, tarball)

        self.manifest = ServiceManifest(self.name, self.title,
                                        'network/vpn/gvpe',
                                        [ 'filesystem', 'network' ],
                                        [ '/etc/gvpe/gvpe.conf' ])

        self.manifest.set_start_command('/usr/sbin/gvpe -l info nodename')
        self.manifest.set_stop_command(':kill -TERM')

    def build(self):
        with self.app.mkwriter('patch', '-p1') as patch:
            patch.write('''
--- a/config.h
+++ b/config.h
@@ -204,7 +204,7 @@
 #define HAVE_PORT_CREATE 1

 /* Define to 1 if you have the <port.h> header file. */
-#define HAVE_PORT_H 1
+/*#define HAVE_PORT_H 1*/

 /* Define to 1 if you have the `putenv' function. */
 #define HAVE_PUTENV 1
--- a/src/ether_emu.C
+++ b/src/ether_emu.C
@@ -37,9 +37,9 @@

 extern struct vpn network;

-struct ether_emu : map<u32, int>
+struct ether_emu : std::map<u32, int>
 {
-  typedef map<u32, int> ipv4map;
+  typedef std::map<u32, int> ipv4map;
   ipv4map ipv4;

   bool tun_to_tap (tap_packet *pkt);
--- a/src/tincd/solaris/device.c
+++ b/src/tincd/solaris/device.c
@@ -21,7 +21,7 @@
 */


-#include "system.h"
+/*#include "system.h"*/

 #include <sys/stropts.h>
 #include <sys/sockio.h>
--- a/src/vpn_tcp.C
+++ b/src/vpn_tcp.C
@@ -49,10 +49,10 @@
 #include <unistd.h>
 #include <fcntl.h>

-#include <map>
-
 #include "netcompat.h"

+#include <map>
+
 #include "vpn.h"

 #if ENABLE_HTTP_PROXY
@@ -69,7 +69,7 @@ struct lt_sockinfo
   }
 };

-struct tcp_si_map : public map<const sockinfo *, tcp_connection *, lt_sockinfo>
+struct tcp_si_map : public std::map<const sockinfo *, tcp_connection *, lt_sockinfo>
 {
   inline void cleaner_cb (ev::timer &w, int revents); ev::timer cleaner;

''')
        Package.build(self)

    def install(self, staging):
        Package.install(self, staging)

        etcdir = join(staging, 'etc/gvpe')
        if not isdir(etcdir):
            os.makedirs(etcdir)

        with open(join(etcdir, 'gvpe.conf'), 'w') as fd:
            fd.write('''udp-port = 50000               # the external port to listen on (configure your firewall)
mtu = 1400                     # minimum MTU of all outgoing interfaces on all hosts
ifname = vpn0                  # the local network device name

node = first                   # just a nickname
hostname = first.example.net   # the DNS name or IP address of the host

node = second
hostname = 133.55.82.9

node = third
hostname = third.example.net
''')

        with open(join(etcdir, 'if-up'), 'w') as fd:
            fd.write('''#!/bin/sh
ip link set $IFNAME address $MAC mtu $MTU up
[ $NODENAME = first  ] && ip addr add 10.0.1.1 dev $IFNAME
[ $NODENAME = second ] && ip addr add 10.0.2.1 dev $IFNAME
[ $NODENAME = third  ] && ip addr add 10.0.3.1 dev $IFNAME
ip route add 10.0.0.0/16 dev $IFNAME
''')

        os.chmod(join(etcdir, 'if-up'), 0755)

##############################################################################

class OpenVPN(Package):
    def __init__(self, app, tarball):
        self.title = 'OpenVPN: Virtual Private Network'

        Package.__init__(self, app, tarball)

        self.manifest = ServiceManifest(self.name, self.title,
                                        'network/vpn/openvpn',
                                        [ 'filesystem', 'network' ],
                                        [ '/etc/openvpn/server.conf' ])

        self.manifest.set_start_command('/usr/sbin/openvpn --daemon --writepid /var/run/openvpn/server.pid --config server.conf --cd /etc/openvpn --script-security 2')
        self.manifest.set_stop_command(':kill -TERM')

    def install(self, staging):
        Package.install(self, staging)

        etcdir = join(staging, 'etc/openvpn')
        if not isdir(etcdir):
            os.makedirs(etcdir)

        shutil.copy(join('sample-config-files', 'server.conf'), etcdir)

        for entry in os.listdir(join('easy-rsa', '2.0')):
            shutil.copy(join('easy-rsa', '2.0', entry), etcdir)

        os.makedirs(join(etcdir, 'keys'))

##############################################################################

class Dovecot(Package):
    def __init__(self, app, tarball):
        self.title = 'Dovecot: Secure IMAP server'

        Package.__init__(self, app, tarball)

        self.manifest = ServiceManifest(self.name, self.title,
                                        'network/imap/dovecot',
                                        [ 'filesystem', 'network' ],
                                        [ '/etc/dovecot/dovecot.conf' ])

        self.manifest.set_start_command('/usr/sbin/dovecot')
        self.manifest.set_stop_command('/usr/sbin/dovecot stop')

    def configure(self):
        self.app.shell('./configure', '--prefix=/usr', '--sysconfdir=/etc',
                       '--localstatedir=/var')

    def build(self):
        opts = ['make',] + list(make_opts) + \
            [ 'prefix=/usr',
              'sysconfdir=/etc',
              'localstatedir=/var',
              'rundir=/var/run/dovecot',
              'statedir=/var/lib/dovecot' ]
        self.app.shell(*opts)

    def install(self, staging):
        opts = ['make',] + \
            [ 'INSTALL=%s' % INSTALL,
              'DESTDIR=%s' % staging,
              'prefix=/usr',
              'sysconfdir=/etc',
              'localstatedir=/var',
              'rundir=/var/run/dovecot',
              'statedir=/var/lib/dovecot',
              'install' ]
        self.app.shell(*opts)

    def extend_prototype(self, prototype, staging):
        prototype.postinstall('''
/usr/sbin/useradd -d /usr/lib/dovecot -s /usr/bin/false dovecot
/usr/sbin/useradd -d /usr/lib/dovecot -s /usr/bin/false dovenull

if ! grep -q ^imap /etc/pam.conf; then
    cat <<EOF >> /etc/pam.conf
imap    auth    requisite       pam_authtok_get.so.1
imap    auth    required        pam_unix_auth.so.1
imap    account requisite       pam_roles.so.1
imap    account required        pam_unix_account.so.1
imap    session required        pam_unix_session.so.1
pop3    auth    requisite       pam_authtok_get.so.1
pop3    auth    required        pam_unix_auth.so.1
pop3    account requisite       pam_roles.so.1
pop3    account required        pam_unix_account.so.1
pop3    session required        pam_unix_session.so.1
EOF
fi
/usr/bin/perl
''')
        prototype.postremove('''
/usr/sbin/userdel dovecot
/usr/sbin/userdel dovenull

/usr/bin/perl -i -ne 'print unless /^(imap|pop3)/;' /etc/pam.conf
''')

##############################################################################

class Glib(Package):
    def __init__(self, app, tarball):
        Package.__init__(self, app, tarball)

    def configure(self):
        self.app.shell('./configure', '--prefix=/usr', '--sysconfdir=/etc',
                       '--disable-dtrace', 'LIBS=-lsocket -lnsl')

    def build(self):
        try:
            self.app.shell('make')  # don't use -jN
        except:
            # These tests fail to build due to a syntax error found in de.po,
            # probably due to an ancient version of gettext in OpenIndiana(?)
            self.app.shell('perl', '-i', '-pe', 's/ tests$//;', 'gio/Makefile')

            self.app.shell('make')  # don't use -jN

##############################################################################

class Privoxy(Package):
    def __init__(self, app, tarball):
        Package.__init__(self, app, tarball)

        self.manifest = ServiceManifest(self.name, self.title,
                                        'network/privoxy',
                                        [ 'filesystem', 'network' ],
                                        [ '/etc/privoxy/config' ])

        self.manifest.set_start_command('/usr/sbin/privoxy --pidfile /var/log/privoxy/privoxy.pid --user webservd.webservd /etc/privoxy/config')
        self.manifest.set_stop_command(':kill -TERM')

    def configure(self):
        self.app.shell('autoconf')
        self.app.shell('autoheader')
        self.app.shell('./configure', '--prefix=/usr', '--sysconfdir=/etc/privoxy',
                       '--localstatedir=/var',
                       'CFLAGS=-O3 -pipe -fomit-frame-pointer -funroll-loops -ffast-math -fno-exceptions',
                       '--enable-compression', '--with-user=webservd',
                       '--with-group=webservd')

##############################################################################

class Squid(Package):
    def __init__(self, app, tarball):
        Package.__init__(self, app, tarball)

        self.manifest = ServiceManifest(self.name, self.title,
                                        'network/squid',
                                        [ 'filesystem', 'network' ],
                                        [ '/etc/squid/squid.conf' ])

        self.manifest.set_start_command('/usr/squid/sbin/squid')
        self.manifest.set_stop_command('/usr/bin/pkill squid')

    def configure(self):
        self.app.shell('./configure', '--prefix=/usr/squid', '--sysconfdir=/etc/squid',
                       '--localstatedir=/var/squid',
                       'CFLAGS=-DNUMTHREADS=60 -O3 -pipe -fomit-frame-pointer -funroll-loops -ffast-math -fno-exceptions',
                       '--enable-async-io',
                       '--enable-useragent-log',
                       '--enable-storeio=aufs,ufs',
                       '--enable-removal-policies=heap,lru',
                       '--with-maxfd=16384',
                       '--enable-poll',
                       '--disable-ident-lookups',
                       '--with-default-user=webservd')

##############################################################################

class Mosh(Package):
    def __init__(self, app, tarball):
        Package.__init__(self, app, tarball)

    def configure(self):
        self.app.shell('./configure', 'CFLAGS=-m64', 'CXXFLAGS=-m64',
                   'LDFLAGS=-m64 -R/usr/gnu/lib/amd64 -L/usr/gnu/lib/amd64',
                   'LIBS=-lsocket -lnsl')

    def build(self):
        with self.app.mkwriter('patch', '-p1') as patch:
            patch.write('''
--- a/src/frontend/Makefile.in
+++ b/src/frontend/Makefile.in
@@ -226 +226 @@ LDADD = ../crypto/libmoshcrypto.a ../network/libmoshnetwork.a ../statesync/libmo
-mosh_server_LDADD = $(LDADD) -lutil
+mosh_server_LDADD = $(LDADD) #-lutil
--- a/src/frontend/mosh-server.cc
+++ b/src/frontend/mosh-server.cc
@@ -27,0 +28 @@
+#include <fcntl.h>
@@ -29,0 +31,3 @@
+#include <sys/stream.h>
+#include <sys/stropts.h>
+#include <sys/syscall.h>
@@ -81,0 +86,81 @@ using namespace std;
+#define forkpty my_forkpty
+
+/* fork_pty() remplacement for Solarisk
+ * This ignore the last two arguments
+ * for the moment
+ */
+
+int
+my_forkpty (int *amaster,
+            char *name,
+            void *unused1,
+            void *unused2)
+{
+  int master, slave;
+  char *slave_name;
+  pid_t pid;
+
+  master = open("/dev/ptmx", O_RDWR);
+  if (master < 0)
+    return -1;
+
+  if (grantpt (master) < 0)
+    {
+      close (master);
+      return -1;
+    }
+
+  if (unlockpt (master) < 0)
+    {
+      close (master);
+      return -1;
+    }
+
+  slave_name = ptsname (master);
+  if (slave_name == NULL)
+    {
+      close (master);
+      return -1;
+    }
+
+  slave = open (slave_name, O_RDWR);
+  if (slave < 0)
+    {
+      close (master);
+      return -1;
+    }
+
+  if (ioctl (slave, I_PUSH, "ptem") < 0
+      || ioctl (slave, I_PUSH, "ldterm") < 0)
+    {
+      close (slave);
+      close (master);
+      return -1;
+    }
+
+  if (amaster)
+    *amaster = master;
+
+  if (name)
+    strcpy (name, slave_name);
+
+  pid = fork ();
+  switch (pid)
+    {
+    case -1: /* Error */
+      return -1;
+    case 0: /* Child */
+      close (master);
+      dup2 (slave, STDIN_FILENO);
+      dup2 (slave, STDOUT_FILENO);
+      dup2 (slave, STDERR_FILENO);
+      return 0;
+    default: /* Parent */
+      close (slave);
+      return pid;
+    }
+
+  return -1;
+
+}
+
@@ -381,3 +466,3 @@ int run_server( const char *desired_ip, const char *desired_port,
-    stdin = fdopen( STDIN_FILENO, "r" );
-    stdout = fdopen( STDOUT_FILENO, "w" );
-    stderr = fdopen( STDERR_FILENO, "w" );
+    //stdin = fdopen( STDIN_FILENO, "r" );
+    //stdout = fdopen( STDOUT_FILENO, "w" );
+    //stderr = fdopen( STDERR_FILENO, "w" );
--- a/src/frontend/stmclient.cc
+++ b/src/frontend/stmclient.cc
@@ -78 +78,6 @@ void STMClient::init( void )
-  cfmakeraw( &raw_termios );
+  /* do the equivalent of cfmakeraw() manually, to build on Solaris */
+  raw_termios.c_iflag &= ~(IGNBRK|BRKINT|PARMRK|ISTRIP|INLCR|IGNCR|ICRNL|IXON);
+  raw_termios.c_oflag &= ~OPOST;
+  raw_termios.c_lflag &= ~(ECHO|ECHONL|ICANON|ISIG|IEXTEN);
+  raw_termios.c_cflag &= ~(CSIZE|PARENB);
+  raw_termios.c_cflag |= CS8;
--- a/src/network/network.cc
+++ b/src/network/network.cc
@@ -207 +207 @@ Connection::Connection( const char *desired_ip, const char *desired_port ) /* se
-bool Connection::try_bind( int socket, uint32_t s_addr, int port )
+bool Connection::try_bind( int socket, uint32_t s__addr, int port )
@@ -211 +211 @@ bool Connection::try_bind( int socket, uint32_t s_addr, int port )
-  local_addr.sin_addr.s_addr = s_addr;
+  local_addr.sin_addr.s_addr = s__addr;
--- a/src/network/network.h
+++ b/src/network/network.h
@@ -80 +80 @@ namespace Network {
-    static bool try_bind( int socket, uint32_t s_addr, int port );
+    static bool try_bind( int socket, uint32_t s__addr, int port );
--- a/src/util/dos_assert.h
+++ b/src/util/dos_assert.h
@@ -38 +38 @@ static void dos_detected( const char *expression, const char *file, int line, co
-   : dos_detected (__STRING(expr), __FILE__, __LINE__, __PRETTY_FUNCTION__ ))
+   : dos_detected (#expr, __FILE__, __LINE__, __PRETTY_FUNCTION__ ))
--- a/src/util/fatal_assert.h
+++ b/src/util/fatal_assert.h
@@ -35 +35 @@ static void fatal_error( const char *expression, const char *file, int line, con
-   : fatal_error (__STRING(expr), __FILE__, __LINE__, __PRETTY_FUNCTION__ ))
+   : fatal_error (#expr, __FILE__, __LINE__, __PRETTY_FUNCTION__ ))
''')
        Package.build(self)

##############################################################################

class GHC(Package):
    def __init__(self, app, tarball):
        Package.__init__(self, app, tarball)

    def configure(self):
        self.app.shell(
            './configure', '--prefix=/usr', '--sysconfdir=/etc',
            '--with-ld=/usr/bin/ld', '--with-gcc=/usr/bin/gcc',
            '--with-nm=/usr/bin/nm', '--with-gmp-includes=/usr/gnu/include',
            '--with-gmp-libraries=/usr/gnu/lib',
            '--with-iconv-includes=/usr/gnu/include',
            '--with-iconv-libraries=/usr/gnu/lib')

    def build(self):
        env = os.environ.copy()
        env['LD_LIBRARY_PATH'] = \
          '%s:/usr/gnu/lib' % (env['LD_LIBRARY_PATH'] or '',)

        self.app.shell('gmake', env=env)

##############################################################################
##############################################################################
##############################################################################

class PkgBuild(CommandLineApp):
    pkgmap = {
        'apcupsd':         Apcupsd,
        'augeas':          Augeas,
        'cmake':           CMake,
        'db':              BerkeleyDB,
        'dovecot':         Dovecot,
        'ghc':             GHC,
        'glib':            Glib,
        'gvpe':            GVPE,
        'netatalk':        Netatalk,
        'ngircd':          Ngircd,
        'openvpn':         OpenVPN,
        'privoxy':         Privoxy,
        'ruby':            Ruby,
        'ruby-enterprise': RubyEnterprise,
        'squid':           Squid,
        'mosh':            Mosh,
    }

    def main(self, *args):
        for path in args:
            found = False
            for key in reversed(sorted(self.pkgmap.keys())):
                if re.match(key + '-', path):
                    self.pkgmap[key](self, path).main()
                    found = True
                    break
            if not found:
                Package(self, path).main()

PkgBuild().run()

sys.exit(0)

### pkgbuild.py ends here
