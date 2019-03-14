# -*- coding: utf-8 -*-
import subprocess
import asyncio
from aiohttp.client_exceptions import ClientOSError, ClientResponseError
import gi
gi.require_version('OSTree', '1.0')
from gi.repository import GLib, Gio, OSTree
from datetime import datetime, timedelta
import os
import os.path
import re
import logging

from fullmetalupdate.updater import AsyncUpdater
from rauc_hawkbit.ddi.client import DDIClient, APIError
from rauc_hawkbit.ddi.client import (
    ConfigStatusExecution, ConfigStatusResult)
from rauc_hawkbit.ddi.deployment_base import (
    DeploymentStatusExecution, DeploymentStatusResult)
from rauc_hawkbit.ddi.cancel_action import (
    CancelStatusExecution, CancelStatusResult)

class FullMetalUpdateDDIClient(AsyncUpdater):
    """
    Client broker communicating via DBUS and HawkBit DDI HTTP
    interface.
    """
    def __init__(self, session, host, ssl, tenant_id, target_name, auth_token,
                 attributes, result_callback, step_callback=None, lock_keeper=None):
        super(FullMetalUpdateDDIClient, self).__init__()

        self.attributes = attributes

        self.logger = logging.getLogger('fullmetalupdate_hawkbit')
        self.ddi = DDIClient(session, host, ssl, auth_token, tenant_id, target_name)
        self.action_id = None

        self.lock_keeper = lock_keeper

    async def start_polling(self, wait_on_error=60):
        """Wrapper around self.poll_base_resource() for exception handling."""

        while True:
            try:
                await self.poll_base_resource()
            except asyncio.CancelledError:
                self.logger.info('Polling cancelled')
                break
            except asyncio.TimeoutError:
                self.logger.warning('Polling failed due to TimeoutError')
            except (APIError, TimeoutError, ClientOSError, ClientResponseError) as e:
                # log error and start all over again
                self.logger.warning('Polling failed with a temporary error: {}'.format(e))
            except:
                self.logger.exception('Polling failed with an unexpected exception:')
            self.action_id = None
            self.logger.info('Retry will happen in {} seconds'.format(
                wait_on_error))
            await asyncio.sleep(wait_on_error)

    async def identify(self, base):
        """Identify target against HawkBit."""
        self.logger.info('Sending identifying information to HawkBit')
        # identify
        await self.ddi.configData(
                ConfigStatusExecution.closed,
                ConfigStatusResult.success, **self.attributes)

    async def cancel(self, base):
        self.logger.info('Received cancelation request')
        # retrieve action id from URL
        deployment = base['_links']['cancelAction']['href']
        match = re.search('/cancelAction/(.+)$', deployment)
        action_id, = match.groups()
        # retrieve stop_id
        stop_info = await self.ddi.cancelAction[action_id]()
        stop_id = stop_info['cancelAction']['stopId']
        # Reject cancel request
        self.logger.info('Rejecting cancelation request')
        await self.ddi.cancelAction[stop_id].feedback(
                CancelStatusExecution.rejected, CancelStatusResult.success, status_details=("Cancelling not supported",))

    async def install(self):
        if self.lock_keeper and not self.lock_keeper.lock(self):
            self.logger.info("Another installation is already in progress, aborting")
            return

    async def process_deployment(self, base):
        """
        Check for deployments, download them, verify checksum and trigger
        RAUC install operation.
        """
        if self.action_id is not None:
            self.logger.info('Deployment is already in progress')
            return

        status_result = DeploymentStatusResult.success

        # retrieve action id and resource parameter from URL
        deployment = base['_links']['deploymentBase']['href']
        match = re.search('/deploymentBase/(.+)\?c=(.+)$', deployment)
        action_id, resource = match.groups()
        # fetch deployment information
        deploy_info = await self.ddi.deploymentBase[action_id](resource)
        reboot_needed = False

        chunks_qty = len(deploy_info['deployment']['chunks'])

        if chunks_qty == 0:
            msg = 'Deployment without chunks found. Ignoring'
            status_execution = DeploymentStatusExecution.closed
            status_result = DeploymentStatusResult.failure
            await self.ddi.deploymentBase[action_id].feedback(
                    status_execution, status_result, [msg])
            raise APIError(msg)
        else:
            msg = "FullMetalUpdate:Proceeding"
            percentage = {"cnt": 0, "of": chunks_qty}
            status_execution = DeploymentStatusExecution.proceeding
            status_result = DeploymentStatusResult.none
            await self.ddi.deploymentBase[action_id].feedback(
                    status_execution, status_result, [msg],
                    percentage=percentage)

        self.action_id = action_id
        rev = None
        autostart = None
        autoremove = None
        for chunk in deploy_info['deployment']['chunks']:
            for meta in chunk['metadata']:
                if meta['key'] == 'rev':
                    rev = meta['value']
                if meta['key'] == 'autostart':
                    autostart = int(meta['value'])
                if meta['key'] == 'autoremove':
                    autoremove = int(meta['value'])
            self.logger.info("Updating chunk part: {}".format(chunk['part']))

            if chunk['part'] == 'os':
                self.logger.info("OS {} v.{} - updating...".format(chunk['name'], chunk['version']))
                res = self.update_system(rev)
                if not res:
                    self.logger.error("OS {} v.{} Deployment failed".format(chunk['name'], chunk['version']))
                    msg = "OS {} v.{} Deployment failed".format(chunk['name'], chunk['version'])
                    status_result = DeploymentStatusResult.failure
                else:
                    self.logger.info("OS {} v.{} Deployment succeed".format(chunk['name'], chunk['version']))
                    msg = "OS {} v.{} Deployment succeed".format(chunk['name'], chunk['version'])
                    status_result = DeploymentStatusResult.success
                    reboot_needed = True
            elif chunk['part'] == 'bApp':
                self.logger.info("App {} v.{} - updating...".format(chunk['name'], chunk['version']))
                res = self.update_container(chunk['name'], rev, autostart, autoremove)
                if not res:
                    self.logger.error("App {} v.{} Deployment failed".format(chunk['name'], chunk['version']))
                    msg = "App {} v.{} Deployment failed".format(chunk['name'], chunk['version'])
                    status_result = DeploymentStatusResult.failure
                else:
                    self.logger.info("App {} v.{} Deployment succeed".format(chunk['name'], chunk['version']))
                    msg = "App {} v.{} Deployment succeed".format(chunk['name'], chunk['version'])
                    status_result = DeploymentStatusResult.success

        status_execution = DeploymentStatusExecution.closed
        if status_result != DeploymentStatusResult.failure:
            msg = "FullMetalUpdate: success"

        status_execution = DeploymentStatusExecution.closed

        await self.ddi.deploymentBase[self.action_id].feedback(
                    status_execution, status_result, [msg])
        if reboot_needed :
            try:
                subprocess.run("reboot")
            except subprocess.CalledProcessError as e:
                self.logger.error("Reboot failed: {}".format(e))
        self.action_id = None

    async def sleep(self, base):
        """Sleep time suggested by HawkBit."""
        sleep_str = base['config']['polling']['sleep']
        self.logger.info('Will sleep for {}'.format(sleep_str))
        t = datetime.strptime(sleep_str, '%H:%M:%S')
        delta = timedelta(hours=t.hour, minutes=t.minute, seconds=t.second)
        await asyncio.sleep(delta.total_seconds())

    async def poll_base_resource(self):
        """Poll DDI API base resource."""
        while True:
            base = await self.ddi()

            if '_links' in base:
                if 'configData' in base['_links']:
                    await self.identify(base)
                if 'deploymentBase' in base['_links']:
                    await self.process_deployment(base)
                if 'cancelAction' in base['_links']:
                    await self.cancel(base)

            await self.sleep(base)
