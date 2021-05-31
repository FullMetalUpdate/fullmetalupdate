How to add targets to Hawkbit
==================================

When using FullMetalUpdate, you may require to work on multiple targets and deploy OSes or
containers on them. In this document, we will go though the steps required to add a new
target on Hawkbit and be able to deploy images on any of them.

Adding the target on Hawkbit's UI
------------------------------------

 1. Go to the cloud directory where Hawkbit is launched (`fullmetalupdate-cloud-demo`) 

 2. Edit `ConfigureServer.sh` and make the following changes :
      - copy the first paragraph of the file, beginning with `# Set up a target...`, and
        paste it right afterwards
      - change `securityToken`, `controllerId` and `name`. For the last two, pick whatever
        you want. For the token, also pick whatever you want, or you can generate a token
        using `date | md5sum`, assuming you're on Linux.

 3. Exit, and execute `./ConfigureServer.sh` (the server needs to be running).

 4. You should see a new target pop up on Hawkbit. Cross-check that the credentials you
    typed at the last step are correct in the information panel of the target.

Configuring your build to connect your target with Hawkbit
---------------------------------------------------------------

There are two ways to make your change on Hawkbit take effect or the target :

  - (*Recommended*) Configure the new credentials from the build directory.
    These changes will be permanent though all the next updates of your target

  - Configure the new credentials on the target directly

If you already deployed your target, we recommend to go thourhg both ways.

Configuring a new target on the build system
---------------------------------------------------

Before proceeding further, **close any instanciation of the build-yocto docker** (any
`./Startbuild.sh bash *`).

 1. Go to your Yocto directory (`fullmetalupdate-yocto-demo`).

 2. Edit the `config.cfg.sample` and make the following changes :

    Fill in `hawkbit_target_name` and `hawkbit_auth_token` using what you have previously
    defined (`controllerId` and `securityToken`).

 3. Exit, and execute `bash ConfigureBuild.sh`.

 4. Execute `./Startbuild.sh fullmetalupdate-os`. It should build the fullmetalupdate
    client again. Then you can flash the new image to your target, which will contain the
    new credentials.

 5. After starting the target, you should see your device's IP in the target's information panel.

Configuring the credentials from the target
--------------------------------------------------

*Note* : these changes will be permanent only if you made the changes on the build system as described in the last section. Otherwise, the following changes will be lost on the next OS update.

If you have already deployed your target and have remote access to it, please follow these steps :

 1. Remount `/usr` as read-write by executing: `mount -o remount,rw /usr`.

 2. Edit the configuration file by executing `vi
    /usr/fullmetalupdate/rauc_hawkbit/config.cfg`.

 3. Fill in `hawkbit_target_name` and `hawkbit_auth_token` using what you have previously
    defined (`controllerId` and `securityToken`).

 4. Restart the FullMetalUpdate client by executing `systemctl restart fullmetalupdate`.

 5. You should see your device's IP in the target's information panel.
