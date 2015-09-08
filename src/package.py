import os
import shutil
import subprocess

from . import config
from . import util
from .builder import BuildHandle, HostBuilder, TargetBuilder


def _empty_dir(self):
    # TODO(ed): This should not delete the actual directory.
    try:
        util.remove(config.DIR_BUILDROOT)
    except FileNotFoundError:
        pass


class HostPackage:

    def __init__(
            self,
            install_directory,
            name,
            version,
            homepage,
            maintainer,
            lib_depends,
            distfiles,
            build_cmd):
        self._install_directory = install_directory
        self._name = name
        self._version = version
        self._distfiles = distfiles
        self._build_cmd = build_cmd

        # Compute the set of transitive library dependencies.
        self._lib_depends = set()
        for dep in lib_depends:
            self._lib_depends.add(dep)
            self._lib_depends |= dep._lib_depends

    def _prepare_buildroot(self):
        # Ensure that all dependencies have been built.
        for dep in self._lib_depends:
            dep.build()

        # Install dependencies into an empty buildroot.
        _empty_dir(config.DIR_BUILDROOT)
        for dep in self._lib_depends:
            dep.extract()

    def build(self):
        # Skip this package if it has been built already.
        if os.path.isdir(self._install_directory):
            return

        # Perform the build inside an empty buildroot.
        self._prepare_buildroot()
        print('BUILD', self._name)
        self._build_cmd(
            BuildHandle(
                HostBuilder(
                    self._install_directory),
                self._name,
                self._version,
                self._distfiles))

    def extract(self):
        # Copy files literally.
        for source_file, target_file in util.walk_files_concurrently(
                self._install_directory, config.DIR_BUILDROOT):
            util.make_parent_dir(target_file)
            util.copy_file(source_file, target_file, False)


class TargetPackage:

    def __init__(
            self,
            install_directory,
            arch,
            name,
            version,
            homepage,
            maintainer,
            host_packages,
            lib_depends,
            build_cmd,
            distfiles):
        self._install_directory = install_directory
        self._arch = arch
        self._name = name
        self._version = version
        self._homepage = homepage
        self._maintainer = maintainer
        self._host_packages = host_packages
        self._build_cmd = build_cmd
        self._distfiles = distfiles

        # Compute the set of transitive library dependencies.
        self._lib_depends = set()
        for dep in lib_depends:
            self._lib_depends.add(dep)
            self._lib_depends |= dep._lib_depends

    def _prepare_buildroot(self, host_depends, lib_depends):
        # Ensure that all dependencies have been built.
        for dep in host_depends:
            package = self._host_packages[dep]
            package.build()
            for depdep in package._lib_depends:
                depdep.build()
        for dep in lib_depends:
            dep.build()

        # Install dependencies into an empty buildroot.
        _empty_dir(config.DIR_BUILDROOT)
        for dep in host_depends:
            package = self._host_packages[dep]
            package.extract()
            for depdep in package._lib_depends:
                depdep.extract()
        prefix = os.path.join(config.DIR_BUILDROOT, self._arch)
        for dep in lib_depends:
            dep.extract(prefix, prefix)

    def build(self):
        # Skip this package if it has been built already.
        if not self._build_cmd or os.path.isdir(self._install_directory):
            return

        # Perform the build inside a buildroot with its dependencies
        # installed in place.
        self._prepare_buildroot(set([
            'binutils', 'cmake', 'llvm', 'make', 'pkgconf',
        ]), self._lib_depends)
        print('BUILD', self._name)
        self._build_cmd(
            BuildHandle(
                TargetBuilder(
                    self._install_directory,
                    self._arch),
                self._name,
                self._version,
                self._distfiles))

    def create_freebsd_package(self):
        # Install just a copy of FreeBSD's pkg(8) into the buildroot,
        # which we can call into to create the package.
        self.build()
        self._prepare_buildroot(set(['pkg']), set())
        print('PKG', self._name)

        # The package needs to be installed in /usr/local/<arch> on the
        # FreeBSD system.
        prefix = os.path.join('/usr/local', self._arch)
        installdir = os.path.join(config.DIR_BUILDROOT, 'install')
        filesdir = os.path.join(installdir, prefix[1:])
        self.extract(filesdir, prefix)

        # Create a manifest file.
        util.make_dir(installdir)
        with open(os.path.join(installdir, '+MANIFEST'), 'w') as f:
            # Preamble.
            # TODO(ed): Encode dependencies.
            f.write(
                'name: %(arch)s-%(name)s\n'
                'version: \"%(version)s\"\n'
                'origin: devel/%(arch)s-%(name)s\n'
                'comment: %(name)s for %(arch)s\n'
                'www: %(homepage)s\n'
                'maintainer: %(maintainer)s\n'
                'prefix: /usr/local\n'
                'desc: %(name)s for %(arch)s\n'
                'abi: FreeBSD:*\n'
                'arch: freebsd:*\n'
                'files {\n' % {
                    'arch': self._arch,
                    'homepage': self._homepage,
                    'maintainer': self._maintainer,
                    'name': self._name,
                    'version': self._version
                })

            # Create entry for every file.
            for path in sorted(util.walk_files(filesdir)):
                if os.path.islink(path):
                    perm = 0o777
                elif (os.lstat(path).st_mode & 0o111) != 0:
                    perm = 0o555
                else:
                    perm = 0o444
                relpath = os.path.join('/', os.path.relpath(path, installdir))
                f.write('  \"/%s\": { perm: 0%o }' % (relpath, perm))

            f.write('}\n')

        # Create the package.
        subprocess.check_call([
            os.path.join(config.DIR_BUILDROOT, 'sbin/pkg'),
            'create',
            '-r', installdir,
            '-m', installdir,
            '-o', config.DIR_BUILDROOT,
        ])
        return os.path.join(
            config.DIR_BUILDROOT, '%s-%s-%s.txz' %
            (self._arch, self._name, self._version))

    def extract(self, path, expandpath):
        for source_file, target_file in util.walk_files_concurrently(
                self._install_directory, path):
            util.make_parent_dir(target_file)
            if target_file.endswith('.template'):
                # File is a template. Expand %%PREFIX%% tags.
                target_file = target_file[:-9]
                with open(source_file, 'r') as f:
                    contents = f.read()
                contents = contents.replace('%%PREFIX%%', expandpath)
                with open(target_file, 'w') as f:
                    f.write(contents)
                shutil.copymode(source_file, target_file)
            else:
                # Regular file. Copy it over literally.
                util.copy_file(source_file, target_file, False)