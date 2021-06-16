#!/bin/sh

CONTAINER_NAME=$1
SOCKET_PATH="/tmp/fullmetalupdate/fullmetalupdate_notify_${CONTAINER_NAME}.sock"

msg="success" 
test -z ${EXIT_CODE} || msg="${SERVICE_RESULT} ${EXIT_CODE} ${EXIT_STATUS}"

test -e ${SOCKET_PATH} && \
    echo ${msg} | socat - UNIX-CONNECT:${SOCKET_PATH} 

exit 0
