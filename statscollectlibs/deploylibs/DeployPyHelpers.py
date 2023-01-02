
# -*- coding: utf-8 -*-
# vim: ts=4 sw=4 tw=100 et ai si
#
# Copyright (C) 2019-2022 Intel Corporation
# SPDX-License-Identifier: BSD-3-Clause
#
# Author: Artem Bityutskiy <artem.bityutskiy@linux.intel.com>

"""This module provides the API for deploying Python helpers."""

import logging
from pathlib import Path
from pepclibs.helperlibs import ClassHelpers, LocalProcessManager, ProjectFiles
from pepclibs.helperlibs.Exceptions import Error, ErrorNotFound
from statscollectlibs.deploylibs import _DeployHelpersBase

_LOG = logging.getLogger()

class DeployPyHelpers(_DeployHelpersBase.DeployHelpersBase):
    """This class provides the API for deploying Python helpers."""

    def _find_deployable(self, deployable):
        """Find and return path to program 'deployable' (on the local host)."""

        with LocalProcessManager.LocalProcessManager() as lpman:
            try:
                deployable_path = ProjectFiles.find_project_helper(self._prjname, deployable,
                                                                   pman=lpman)
            except ErrorNotFound as err1:
                _LOG.debug(err1)

                try:
                    subpath = _DeployHelpersBase.HELPERS_SRC_SUBPATH / deployable / deployable
                    what = f"the '{deployable}' python program"
                    deployable_path = ProjectFiles.find_project_data(self._prjname, subpath,
                                                                     what=what, pman=lpman)
                except ErrorNotFound as err2:
                    errmsg = str(err1).capitalize() + "\n" + str(err2).capitalize()
                    raise Error(errmsg) from err2

            deployable_path = lpman.abspath(deployable_path)

        return deployable_path

    @staticmethod
    def _get_pyhelper_dependencies(script_path):
        """
        Find and return a python helper script (pyhelper) dependencies. An example of such a
        dependency would be:
            /usr/lib/python3.9/site-packages/helperlibs/Trivial.py
        """

        # All pyhelpers implement the '--print-module-paths' option, which prints the dependencies.
        cmd = f"{script_path} --print-module-paths"
        with LocalProcessManager.LocalProcessManager() as lpman:
            stdout, _ = lpman.run_verify(cmd)
        return [Path(path) for path in stdout.splitlines()]

    def _create_standalone_pyhelper(self, pyhelper_path, outdir):
        """
        Create a standalone version of a python program. The arguments are as follows.
          * pyhelper_path - path to the python helper program on the local system. This method will
                            execute it on with the '--print-module-paths' option, which this it is
                            supposed to support. This option will provide the list of modules the
                            python helper program depends on.
          * outdir - path to the output directory. The standalone version of the script will be
                     saved in this directory under the "'pyhelper'.standalone" name.
        """

        import zipfile # pylint: disable=import-outside-toplevel

        pyhelper = pyhelper_path.name

        deps = self._get_pyhelper_dependencies(pyhelper_path)

        # Create an empty '__init__.py' file. We will be copy it to the sub-directories of the
        # dependencies.
        init_path = outdir / "__init__.py"
        try:
            with init_path.open("w+"):
                pass
        except OSError as err:
            msg = Error(err).indent(2)
            raise Error(f"failed to create file '{init_path}:\n{msg}'") from None

        try:
            # pylint: disable=consider-using-with
            fobj = zipobj = None

            # Start creating the stand-alone version of the python helper script: create an empty
            # file and write # python shebang there.
            standalone_path = outdir / f"{pyhelper}.standalone"
            try:
                fobj = standalone_path.open("bw+")
                fobj.write("#!/usr/bin/python3\n".encode("utf-8"))
            except OSError as err:
                msg = Error(err).indent(2)
                raise Error(f"failed to create and initialize file '{standalone_path}:\n"
                            f"{msg}") from err

            # Create a zip archive in the 'standalone_path' file. The idea is that this file will
            # start with python shebang, and then include compressed version the script and its
            # dependencies. Python interpreter is smart and can run such zip archives.
            try:
                zipobj = zipfile.ZipFile(fobj, "w", compression=zipfile.ZIP_DEFLATED)
            except Exception as err:
                msg = Error(err).indent(2)
                raise Error(f"failed to initialize a zip archive from file "
                            f"'{standalone_path}':\n{msg}") from err

            # Make 'zipobj' raises exceptions of type 'Error', so that we do not have to wrap every
            # 'zipobj' operation into 'try/except'.
            zipobj = ClassHelpers.WrapExceptions(zipobj)

            # Put the python helper script to the archive under the '__main__.py' name.
            zipobj.write(pyhelper_path, arcname="./__main__.py")

            pkgdirs = set()

            for src in deps:
                # Form the destination path. It is just part of the source path staring from the
                # 'statscollectlibs' of 'helperlibs' components.
                try:
                    idx = src.parts.index("statscollectlibs")
                except ValueError:
                    try:
                        idx = src.parts.index("helperlibs")
                    except ValueError:
                        raise Error(f"python helper script '{pyhelper}' has bad dependency '{src}' "
                                    f"- the path does not have the 'statscollectlibs' or "
                                    f"'helperlibs' component in it.") from None

                dst = Path(*src.parts[idx:])
                zipobj.write(src, arcname=dst)

                # Collect all directory paths present in the dependencies. They are all python
                # packages and we'll have to ensure we have the '__init__.py' file in each of the
                # sub-directory.
                pkgdir = dst.parent
                for idx, _ in enumerate(pkgdir.parts):
                    pkgdirs.add(Path(*pkgdir.parts[:idx+1]))

            # Ensure the '__init__.py' file is present in all sub-directories.
            zipped_files = {Path(name) for name in zipobj.namelist()}
            for pkgdir in pkgdirs:
                path = pkgdir / "__init__.py"
                if path not in zipped_files:
                    zipobj.write(init_path, arcname=pkgdir / "__init__.py")
        finally:
            if zipobj:
                zipobj.close()
            if fobj:
                fobj.close()

        # Make the standalone file executable.
        try:
            mode = standalone_path.stat().st_mode | 0o777
            standalone_path.chmod(mode)
        except OSError as err:
            msg = Error(err).indent(2)
            raise Error(f"cannot change '{standalone_path}' file mode to {oct(mode)}:\n"
                        f"{msg}") from err

    def _prepare(self, helpersrc, helpers):
        """
        Build and prepare python helpers for deployment. The arguments are as follows:
          * helpersrc - path to the helpers base directory on the controller.
          * helpers - the names of Python helpers to deploy.
        """

        # Copy python helpers to the temporary directory on the controller.
        for pyhelper in helpers:
            srcdir = helpersrc / pyhelper
            _LOG.debug("copying python helper %s:\n  '%s' -> '%s'", pyhelper, srcdir, self._ctmpdir)
            self._cpman.rsync(srcdir, self._ctmpdir, remotesrc=False, remotedst=False)

        # Build stand-alone version of every python helper.
        for pyhelper in helpers:
            _LOG.info("Building a stand-alone version of '%s'", pyhelper)
            basedir = self._ctmpdir / pyhelper
            for deployable in self._deployables:
                deployable_path = self._find_deployable(deployable)
                self._create_standalone_pyhelper(deployable_path, basedir)

        # And copy the "standalone-ized" version of python helpers to the SUT.
        if self._spman.is_remote:
            for pyhelper in helpers:
                srcdir = self._ctmpdir / pyhelper
                _LOG.debug("copying python helper '%s' to %s:\n  '%s' -> '%s'",
                           pyhelper, self._spman.hostname, srcdir, self._stmpdir)
                self._spman.rsync(srcdir, self._stmpdir, remotesrc=False, remotedst=True)

    def __init__(self, prjname, toolname, deployables, spman, bpman, cpman, stmpdir, btmpdir,
                 ctmpdir, debug=False):
        """
        Class constructor. Arguments are the same as in '_DeployHelpersBase.DeployHelpersBase()'
        except for:
         * deployables - the names of deployables to deploy.
        """

        self._deployables = deployables

        what = f"{toolname} python helpers"
        super().__init__(prjname, toolname, what, spman, bpman, stmpdir, btmpdir, cpman=cpman,
                         ctmpdir=ctmpdir, debug=debug)
