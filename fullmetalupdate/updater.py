# -*- coding: utf-8 -*-

import asyncio
import logging
import traceback
import gi, os
import shutil

gi.require_version("OSTree", "1.0")
from gi.repository import OSTree, GLib, Gio
from pydbus import SystemBus

PATH_APPS = '/apps'
PATH_REPO_OS = '/ostree/repo/'
PATH_REPO_APPS = PATH_APPS + '/ostree_repo'
PATH_SYSTEMD_UNITS = '/etc/systemd/system/'
VALIDATE_CHECKOUT = 'CheckoutDone'
FILE_AUTOSTART = 'auto.start'
CONTAINER_UID = 1000
CONTAINER_GID = 1000

class DBUSException(Exception):
    pass


class AsyncUpdater(object):
    def __init__(self):
        self.logger = logging.getLogger('fullmetalupdate_container_updater')

        bus = SystemBus()
        self.systemd = bus.get('.systemd1')

        self.sysroot = OSTree.Sysroot.new_default()
        self.sysroot.load(None)

        [res,repo] = self.sysroot.get_repo()
        self.repo_os = repo
        
        self.repo_containers = OSTree.Repo.new(Gio.File.new_for_path(PATH_REPO_APPS))
        if os.path.exists(PATH_REPO_APPS):
            self.logger.info("Preinstalled OSTree for containers, we use it")
            self.repo_containers.open(None)
        else:
            self.logger.info("No preinstalled OSTree for containers, we create one")
            self.repo_containers.create(OSTree.RepoMode.BARE_USER_ONLY, None)

    def init_ostree_remotes(self, ostree_remote_attributes):
        res = True
        self.ostree_remote_attributes = ostree_remote_attributes
        opts = GLib.Variant('a{sv}', {'gpg-verify':GLib.Variant('b', ostree_remote_attributes['gpg-verify'])})
        try:
            self.logger.info("Initalize remotes for the OS ostree: {}".format(ostree_remote_attributes['name']))
            if not ostree_remote_attributes['name'] in self.repo_os.remote_list():
                self.repo_os.remote_add(ostree_remote_attributes['name'],
                                ostree_remote_attributes['url'],
                                opts, None)
            self.remote_name_os = ostree_remote_attributes['name'] 

            [_,refs] = self.repo_containers.list_refs(None, None)

            self.logger.info("Initalize remotes for the containers ostree: {}".format(refs))
            for refspec in refs:
                remote_name = refspec
                if not remote_name in self.repo_containers.remote_list():
                    self.logger.info("We had the remote: {}".format(remote_name))
                    self.repo_containers.remote_add(remote_name,
                                ostree_remote_attributes['url'],
                                opts, None)

        except GLib.Error as e:
            self.logger.error("OSTRee remote initialization failed ({})".format(str(e)))
            res = False
   
        return res

    def init_checkout_existing_containers(self):
        res = True
        self.logger.info("Getting refs from repo:{}".format(PATH_REPO_APPS))

        try:
            [_,refs] = self.repo_containers.list_refs(None, None)
            for refspec in refs:
                container_name = refspec
                if not os.path.isfile(PATH_APPS + '/' + container_name + '/' + VALIDATE_CHECKOUT):
                    res = self.checkout_container(container_name, None)
                if not res:
                    self.logger.error("Error when checking out container:{}".format(container_name))
                    break
                res = self.create_and_start_unit(container_name)
                if not res:
                    self.logger.error("Error when enablig/starting the systemd unit for container:{}".format(container_name))
                    break
        except GLib.Error as e:
            self.logger.error("Error checking out containers repo:{}".format(e))
            res = False
        return res

    def start_unit(self, container_name):
        self.logger.info("Enable the container {}".format(container_name))
        self.systemd.EnableUnitFiles([container_name + '.service'], False, False)
        self.logger.info("Since FILE_AUTOSTART is present, start the container using systemd")
        self.systemd.StartUnit(container_name + '.service', "fail")

    def stop_unit(self, container_name):
        self.logger.info("Since FILE_AUTOSTART is not present, stop the container using systemd")
        self.systemd.StopUnit(container_name + '.service', "fail")
        self.logger.info("Disable the container {}".format(container_name))
        self.systemd.DisableUnitFiles([container_name + '.service'], False)
        
    def create_and_start_unit(self, container_name):
        res = True
        try:
            self.logger.info("Copy the service file to /etc/systemd/system/{}.service".format(container_name))
            shutil.copy(PATH_APPS + '/' + container_name + '/systemd.service', PATH_SYSTEMD_UNITS + container_name + '.service')
            if os.path.isfile(PATH_APPS + '/' + container_name + '/' + FILE_AUTOSTART):
                self.start_unit(container_name)
        except GLib.Error as e:
            self.logger.error("Error starting unit {} :{}".format(container_name, e))
            res = False
        return res

    def update_container(self, container_name, rev_number, autostart, autoremove):
        """
        Update the given container. To do so, it checks the container status (active, loaded, etc...)
        If necessary, container is stopped. Files are checked out to the installation folder.
        And container is started again.
        """
        # if a container is named 'container-hello-world-package', its service will be 'container-hello-world.service'
        service_name = None
        for unit in self.systemd.ListUnits():
            # full unit : ('container-hello-world.service', 'container-hello-world-imx6qdlsabresd container service', 'loaded', 'failed', 'failed', '', '/org/freedesktop/systemd1/unit/wtk_2dnodejs_2ddemo_2eservice', 0, '', '/')
            if unit[0].replace('.service', '') in container_name:
                self.logger.debug("Service found!")
                service_name = unit[0]
                break

        try:
            if service_name is None:
                # New service added, we need to connect to its remote
                opts = GLib.Variant('a{sv}', {'gpg-verify':GLib.Variant('b', self.ostree_remote_attributes['gpg-verify'])})
                # Check if this container was not installed previously
                if not container_name in self.repo_containers.remote_list():
                    self.logger.info("New container added to the target, we install the remote: {}".format(container_name))
                    self.repo_containers.remote_add(container_name,
                                self.ostree_remote_attributes['url'],
                                opts, None)
                else:
                    self.logger.info("New container {} added to the target but the remote already exists, we do nothing".format(container_name))


            progress = OSTree.AsyncProgress.new()
            progress.connect('changed', OSTree.Repo.pull_default_console_progress_changed, None)

            opts = GLib.Variant('a{sv}', {'flags':GLib.Variant('i', OSTree.RepoPullFlags.NONE), 'refs': GLib.Variant('as', (rev_number,))})
            self.logger.error("Pulling {} from OSTree repo, refs ({})".format(container_name, rev_number))
            res = self.repo_containers.pull_with_options(container_name, opts, progress, None)
            progress.finish()
        except GLib.Error as e:
            self.logger.error("Pulling {} from OSTree repo failed ({})".format(container_name, str(e)))
            res = False
        else:
            if not service_name is None:
                self.logger.info("Stop the container {}".format(container_name))
                self.stop_unit(container_name)

            self.logger.info("Checking out the new container {} rev {}".format(container_name, rev_number))
            res = self.checkout_container(container_name, rev_number)

            self.logger.info("Update the UID and GID of the rootfs")
            os.chown(PATH_APPS + '/' + container_name, CONTAINER_UID, CONTAINER_GID)
            for dirpath, dirnames, filenames in os.walk(PATH_APPS + '/' + container_name):
                for dname in dirnames:
                    os.lchown(os.path.join(dirpath, dname), CONTAINER_UID, CONTAINER_GID)
                for fname in filenames:
                    os.lchown(os.path.join(dirpath, fname), CONTAINER_UID, CONTAINER_GID)

            if not res:
                self.logger.error("Checking out container {} Failed!".format(container_name))
            else:
                if autoremove == 1:
                    self.logger.info("Remove the directory: {}".format(PATH_APPS + '/' + container_name))
                    shutil.rmtree(PATH_APPS + '/' + container_name)
                    res = True
                else:
                    if service_name is None:
                        self.logger.info("First installation of the container {} on the system, we create and start the service".format(container_name))
                        res = self.create_and_start_unit(container_name)
                    else:
                        if autostart == 1:
                            if not os.path.isfile(PATH_APPS + '/' + container_name + '/' + FILE_AUTOSTART):
                                open(PATH_APPS + '/' + container_name + '/' + FILE_AUTOSTART, 'a').close()
                            self.start_unit(container_name)
                        else:
                            if os.path.isfile(PATH_APPS + '/' + container_name + '/' + FILE_AUTOSTART):
                                os.remove(PATH_APPS + '/' + container_name + '/' + FILE_AUTOSTART)
                        res= True
        return res

    def checkout_container(self, container_name, rev_number):
        res = True
        rootfs_fd = None
        try:
            options = OSTree.RepoCheckoutAtOptions()
            options.overwrite_mode = OSTree.RepoCheckoutOverwriteMode.UNION_IDENTICAL
            options.process_whiteouts = True
            options.bareuseronly_dirs = True
            options.no_copy_fallback = True
            options.mode = OSTree.RepoCheckoutMode.USER

            self.logger.info("Getting rev from repo:{}".format(container_name))
            if rev_number == None:
                rev = self.repo_containers.resolve_rev(container_name, False)[1]
            else:
                rev = rev_number
            self.logger.info("Rev value:{}".format(rev))
            if os.path.isdir(PATH_APPS + '/' + container_name):
                shutil.rmtree(PATH_APPS + '/' + container_name)
            os.mkdir(PATH_APPS + '/' + container_name)
            self.logger.info("Create directory {}/{}".format(PATH_APPS, container_name))
            rootfs_fd = os.open(PATH_APPS + '/' + container_name, os.O_DIRECTORY)
            res = self.repo_containers.checkout_at(options, rootfs_fd, PATH_APPS + '/' + container_name, rev)
            open(PATH_APPS + '/' + container_name + '/' + VALIDATE_CHECKOUT, 'a').close()

        except GLib.Error as e:
            self.logger.error("Checking out {} failed ({})".format(container_name, str(e)))
            res = False
        if rootfs_fd != None:
            os.close(rootfs_fd)
        return res

    def update_system(self, rev_number):
        """
        Update the whole system using deploy mechanism.
        name is the repo branch name and target name
        """
        try:
            deployments = self.sysroot.get_deployments()
            if deployments is None:
                self.logger.error("Not booted in an OSTree system")
                return

            first_deployment = deployments[0]
            starting_revision = first_deployment.get_csum()
            osname = first_deployment.get_osname()
            self.logger.info("Using OS {} revision {}".format(osname, starting_revision))

            progress = OSTree.AsyncProgress.new()
            progress.connect('changed', OSTree.Repo.pull_default_console_progress_changed, None)
            
            opts = GLib.Variant('a{sv}', {'flags':GLib.Variant('i', OSTree.RepoPullFlags.NONE), 'refs': GLib.Variant('as', (rev_number,))})
            res = self.repo_os.pull_with_options(self.remote_name_os, opts, progress, None)
            progress.finish()

            self.logger.info("Upgrader pulled rev: {} osname: {}".format(rev_number, self.remote_name_os))
            [res, checksum] = self.repo_os.resolve_rev(rev_number, False)
            origin = first_deployment.get_origin()

            [res, _] = self.sysroot.stage_tree(osname, checksum, origin, first_deployment, None, None)

            self.logger.info("Write the new deployment")
            self.sysroot.cleanup(None)

            if res:
                self.logger.info("Deployed")
                if res is True:
                    self.logger.warning("Deploying {}: operation succeed (modifications will be taken into account after reboot".format(self.remote_name_os))
                else:
                    self.logger.error("Deploying {}: operation FAILED".format(self.remote_name_os))

            return res

        except GLib.Error as e:
            self.logger.error("Update System {} from OSTree repo failed ({})".format(self.remote_name_os, str(e)))
            return False
