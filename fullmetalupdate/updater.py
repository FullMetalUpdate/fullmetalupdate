# -*- coding: utf-8 -*-

import asyncio
import logging
import traceback
import gi, os
import shutil
import subprocess
import json

gi.require_version("OSTree", "1.0")
from gi.repository import OSTree, GLib, Gio
from pydbus import SystemBus

PATH_APPS = '/apps'
PATH_REPO_OS = '/ostree/repo/'
PATH_REPO_APPS = PATH_APPS + '/ostree_repo'
PATH_SYSTEMD_UNITS = '/etc/systemd/system/'
PATH_CURRENT_REVISIONS = '/var/local/fullmetalupdate/current_revs.json'
VALIDATE_CHECKOUT = 'CheckoutDone'
FILE_AUTOSTART = 'auto.start'
CONTAINER_UID = 1000
CONTAINER_GID = 1000
OSTREE_DEPTH = 1

class DBUSException(Exception):
    pass


class AsyncUpdater(object):
    def __init__(self):
        self.logger = logging.getLogger('fullmetalupdate_container_updater')

        self.mark_os_successful()

        bus = SystemBus()
        self.systemd = bus.get('.systemd1')

        self.sysroot = OSTree.Sysroot.new_default()
        self.sysroot.load(None)
        self.logger.info("Cleaning the sysroot")
        self.sysroot.cleanup(None)

        [res,repo] = self.sysroot.get_repo()
        self.repo_os = repo

        self.remote_name_os = None
        self.repo_containers = OSTree.Repo.new(Gio.File.new_for_path(PATH_REPO_APPS))
        if os.path.exists(PATH_REPO_APPS):
            self.logger.info("Preinstalled OSTree for containers, we use it")
            self.repo_containers.open(None)
        else:
            self.logger.info("No preinstalled OSTree for containers, we create one")
            self.repo_containers.create(OSTree.RepoMode.BARE_USER_ONLY, None)

    def mark_os_successful(self):
        """
        This method marks the currently running OS as successful by setting the init_var u-boot
        environment variable to 1.
        Returns :
         - True if the variable was successfully set
         - False otherwise
        """
        try:
            mark_successful = subprocess.call(["fw_setenv", "success", "1"])

            if mark_successful == 0:
                self.logger.info("Setting success u-boot environment variable to 1 succeeded")
            else:
                self.logger.error("Setting success u-boot environment variable to 1 failed")

        except subprocess.CalledProcessError as e:
            self.logger.error("Ostree rollback post-process commands failed ({})".format(str(e)))

    def check_for_rollback(self, revision):
        """
        Function used to execute the different commands needed for the rollback to be
        effective.
        We check :
         - if the booted deployment's revision matches the server's revision
         - if so, check if there is a pending deployment (meaning we've rollbacked) and
           undeploy it.
        Returns :
         - True when the system has rollbacked
         - False otherwise
        """
        try:
            has_rollbacked = False

            # returns [pendings deployments, rollback deployments]
            deployments = self.sysroot.query_deployments_for(None)

            # the deployment we are booted on
            booted_deployment_rev = self.sysroot.get_booted_deployment().get_csum()

            if (booted_deployment_rev != revision):
                has_rollbacked = True
                self.logger.warning("The system rollbacked. Checking if we needed to undeploy")
                if (deployments[0] is not None):
                    self.logger.info("There is a pending deployment. Undeploying...")
                    # 0 is the index of the pending deployment (if there is one)
                    if subprocess.call(["ostree", "admin", "undeploy", "0"]) != 0:
                        self.logger.error("Undeployment failed")
                    else:
                        self.logger.info("Undeployment successful")
            else:
                self.logger.info("No undeployment needed")

            return has_rollbacked

        except subprocess.CalledProcessError as e:
            self.logger.error("Ostree rollback post-process commands failed ({})".format(str(e)))
            return False

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
            for ref in refs:
                remote_name = ref.split(':')[0]          
                if not remote_name in self.repo_containers.remote_list():
                    self.logger.info("We had the remote: {}".format(remote_name))
                    self.repo_containers.remote_add(remote_name,
                                ostree_remote_attributes['url'],
                                opts, None)

        except GLib.Error as e:
            self.logger.error("OSTRee remote initialization failed ({})".format(str(e)))
            res = False
   
        return res

    def set_current_revision(self, container_name, rev):
        """
        This method write rev into a json file containing the current working rev for the
        containers.
        """
        try:
            with open(PATH_CURRENT_REVISIONS, "r") as f:
                current_revs = json.load(f)
            current_revs.update({container_name: rev})
            with open(PATH_CURRENT_REVISIONS, "w") as f:
                json.dump(current_revs, f, indent=4)
        except FileNotFoundError:
            with open(PATH_CURRENT_REVISIONS, "w") as f:
                current_revs = {container_name: rev}
                json.dump(current_revs, f, indent=4)

    def get_previous_rev(self, container_name):
        """
        This method returns the previous working revision of a notify container.

        If the file is not found of the container name is not found in the file,
        it will return None. This means this is the first installation of the container.
        """
        try:
            with open(PATH_CURRENT_REVISIONS, "r") as f:
                current_revs = json.load(f)
            return current_revs[container_name]
        except (FileNotFoundError, KeyError):
            return None

    def init_checkout_existing_containers(self):
        res = True
        self.logger.info("Getting refs from repo:{}".format(PATH_REPO_APPS))

        try:
            [_,refs] = self.repo_containers.list_refs(None, None)
            for ref in refs:
                container_name = ref.split(':')[1]
                if not os.path.isfile(PATH_APPS + '/' + container_name + '/' + VALIDATE_CHECKOUT):
                    self.checkout_container(container_name, None)
                if not res:
                    self.logger.error("Error when checking out container:{}".format(container_name))
                    break
                self.create_and_start_unit(container_name)
        except (GLib.Error, Exception) as e:
            self.logger.error("Error checking out containers repo:{}".format(e))
            res = False
        return res

    def start_unit(self, container_name):
        self.logger.info("Enable the container {}".format(container_name))
        self.systemd.EnableUnitFiles([container_name + '.service'], False, False)
        self.logger.info("Since FILE_AUTOSTART is present, start the container using systemd")
        self.systemd.StartUnit(container_name + '.service', "replace")

    def stop_unit(self, container_name):
        self.logger.info("Since FILE_AUTOSTART is not present, stop the container using systemd")
        self.systemd.StopUnit(container_name + '.service', "replace")
        self.logger.info("Disable the container {}".format(container_name))
        self.systemd.DisableUnitFiles([container_name + '.service'], False)

    def create_and_start_unit(self, container_name):
        self.logger.info("Copy the service file to /etc/systemd/system/{}.service".format(container_name))
        shutil.copy(PATH_APPS + '/' + container_name + '/systemd.service', PATH_SYSTEMD_UNITS + container_name + '.service')
        if os.path.isfile(PATH_APPS + '/' + container_name + '/' + FILE_AUTOSTART):
            self.start_unit(container_name)

    def pull_ostree_ref(self, is_container, ref_sha, ref_name=None):
        """
        Wrapper method to pull a ref from an ostree remote.

        Parameters:
        is_container (bool): set to True if you are pulling for a container,
                             set to False for the OS
        ref_sha (str): the ref commit sha to pull
        ref_name (str): the ref name (can be the name of the container, if None, the OS
                        name will be set)
        """
        res = True

        if is_container:
            repo = self.repo_containers
        else:
            repo = self.repo_os
            ref_name = self.remote_name_os

        try:
            progress = OSTree.AsyncProgress.new()
            progress.connect('changed', OSTree.Repo.pull_default_console_progress_changed, None)

            opts = GLib.Variant('a{sv}', {'flags':GLib.Variant('i', OSTree.RepoPullFlags.NONE),
                                          'refs': GLib.Variant('as', (ref_sha,)),
                                          'depth': GLib.Variant('i', OSTREE_DEPTH)})
            self.logger.info("Pulling remote {} from OSTree repo ({})".format(ref_name, ref_sha))
            res = repo.pull_with_options(ref_name, opts, progress, None)
            progress.finish()
            self.logger.info("Upgrader pulled {} from OSTree repo ({})".format(ref_name, ref_sha))
        except GLib.Error as e:
            self.logger.error("Pulling {} from OSTree repo failed ({})".format(ref_name, str(e)))
            raise
        if not res:
            raise Exception("Pulling {} failed (returned False)".format(ref_name))

    def init_container_remote(self, container_name):
        """
        If the container does not exist, initialize its remote.

        Parameters:
        container_name (str): name of the container
        """

        # returns [('container-hello-world.service', 'description', 'loaded', 'failed', 'failed', '', '/org/freedesktop/systemd1/unit/wtk_2dnodejs_2ddemo_2eservice', 0, '', '/')]
        service = self.systemd.ListUnitsByNames([container_name + '.service'])

        try:
            if (service[0][2] == 'not-found'):
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
        except GLib.Error as e:
            self.logger.error("Initializing {} remote failed ({})".format(container_name, str(e)))
            raise

    def update_container_ids(self, container_name):

        self.logger.info("Update the UID and GID of the rootfs")
        os.chown(PATH_APPS + '/' + container_name, CONTAINER_UID, CONTAINER_GID)
        for dirpath, dirnames, filenames in os.walk(PATH_APPS + '/' + container_name):
            for dname in dirnames:
                os.lchown(os.path.join(dirpath, dname), CONTAINER_UID, CONTAINER_GID)
            for fname in filenames:
                os.lchown(os.path.join(dirpath, fname), CONTAINER_UID, CONTAINER_GID)

    def handle_unit(self, container_name, autostart, autoremove):

        if autoremove == 1:
            self.logger.info("Remove the directory: {}".format(PATH_APPS + '/' + container_name))
            shutil.rmtree(PATH_APPS + '/' + container_name)
        else:
            service = self.systemd.ListUnitsByNames([container_name + '.service'])
            if service[0][2] == 'not-found':
                self.logger.info("First installation of the container {} on the system, we create and start the service".format(container_name))
                self.create_and_start_unit(container_name)
            else:
                if autostart == 1:
                    if not os.path.isfile(PATH_APPS + '/' + container_name + '/' + FILE_AUTOSTART):
                        open(PATH_APPS + '/' + container_name + '/' + FILE_AUTOSTART, 'a').close()
                    self.start_unit(container_name)
                else:
                    if os.path.isfile(PATH_APPS + '/' + container_name + '/' + FILE_AUTOSTART):
                        os.remove(PATH_APPS + '/' + container_name + '/' + FILE_AUTOSTART)

    def checkout_container(self, container_name, rev_number):

        service = self.systemd.ListUnitsByNames([container_name + '.service'])
        if (service[0][2] != 'not-found'):
            self.logger.info("Stop the container {}".format(container_name))
            self.stop_unit(container_name)

        res = True
        rootfs_fd = None
        try:
            options = OSTree.RepoCheckoutAtOptions()
            options.overwrite_mode = OSTree.RepoCheckoutOverwriteMode.UNION_IDENTICAL
            options.process_whiteouts = True
            options.bareuseronly_dirs = True
            options.no_copy_fallback = True
            options.mode = OSTree.RepoCheckoutMode.USER

            self.logger.info("Getting rev from repo:{}".format(container_name + ':' + container_name))

            if rev_number == None:
                rev = self.repo_containers.resolve_rev(container_name + ':' + container_name, False)[1]
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
            raise
        if rootfs_fd != None:
            os.close(rootfs_fd)
        if not res:
            raise Exception("Checking out {} failed (returned False)")

    def ostree_stage_tree(self, rev_number):
        """
        Wrapper around sysroot.stage_tree().
        """
        try:
            booted_dep = self.sysroot.get_booted_deployment()
            if booted_dep is None:
                raise Exception("Not booted in an OSTree system")
            [_, checksum] = self.repo_os.resolve_rev(rev_number, False)
            origin = booted_dep.get_origin()
            osname = booted_dep.get_osname()

            [res, _] = self.sysroot.stage_tree(osname, checksum, origin, booted_dep, None, None)

            self.logger.info("Staged the new OS tree. The new deployment will be ready after a reboot")

        except GLib.Error as e:
            self.logger.error("Failed while staging new OS tree ({})".format(e))
            raise
        if not res:
            raise Exception("Failed while staging new OS tree (returned False)")

    def delete_init_var(self):
        """
        This method delete u-boot's environment variable init_var, to restart the rollback
        procedure.
        """
        try:
            self.logger.info("Deleting init_var u-boot environment variable")
            if subprocess.call(["fw_setenv", "init_var"]) != 0:
                self.logger.error("Deleting init_var variable from u-boot environment failed")
            else:
                self.logger.info("Deleting init_var variable from u-boot environment succeeded")
        except subprocess.CalledProcessError as e:
            self.logger.error("Deleting init_var variable from u-boot environment failed ({})".format(e))
            raise
