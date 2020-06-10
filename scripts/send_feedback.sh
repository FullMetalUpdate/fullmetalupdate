#!/bin/sh

SOCKET_PATH='/tmp/fullmetalupdate/fullmetalupdate_notify.sock'

msg="success" 
test -z ${EXIT_CODE} || msg="${SERVICE_RESULT} ${EXIT_CODE} ${EXIT_STATUS}"

test -e ${SOCKET_PATH} && \
    echo ${msg} | socat - UNIX-CONNECT:${SOCKET_PATH} 

exit 0
