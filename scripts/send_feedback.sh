#!/bin/sh

msg="success" 
test -z ${EXIT_CODE} || msg="${SERVICE_RESULT} ${EXIT_CODE} ${EXIT_STATUS}"

test -e /tmp/fullmetalupdate/fullmetalupdate_notify.sock && \
    echo ${msg} | socat - UNIX-CONNECT:/tmp/fullmetalupdate/fullmetalupdate_notify.sock

exit 0
