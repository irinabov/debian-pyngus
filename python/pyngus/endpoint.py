#    Licensed to the Apache Software Foundation (ASF) under one
#    or more contributor license agreements.  See the NOTICE file
#    distributed with this work for additional information
#    regarding copyright ownership.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import logging
import proton

LOG = logging.getLogger(__name__)

_PROTON_VERSION = (int(getattr(proton, "VERSION_MAJOR", 0)),
                   int(getattr(proton, "VERSION_MINOR", 0)))


class Endpoint(object):
    """AMQP Endpoint state machine."""

    # Endpoint States:
    STATE_UNINIT = 0  # initial state
    STATE_PENDING = 1  # local opened, waiting for remote to open
    STATE_REQUESTED = 2  # remote opened, waiting for local to open
    STATE_CANCELLED = 3  # local closed before remote opened
    STATE_ABANDONED = 4  # remote closed before local opened
    STATE_ACTIVE = 5
    STATE_NEED_CLOSE = 6  # remote closed, waiting for local close
    STATE_CLOSING = 7  # locally closed, pending remote close
    STATE_CLOSED = 8  # terminal state
    STATE_ERROR = 9  # unexpected state transition

    STATE_NAMES = ["STATE_UNINIT", "STATE_PENDING", "STATE_REQUESTED",
                   "STATE_CANCELLED", "STATE_ABANDONED", "STATE_ACTIVE",
                   "STATE_NEED_CLOSE", "STATE_CLOSING", "STATE_CLOSED",
                   "STATE_ERROR"]

    # Events:
    # These correspond to endpoint events generated by the Proton Engine

    LOCAL_OPENED = 0
    LOCAL_CLOSED = 1
    REMOTE_OPENED = 2
    REMOTE_CLOSED = 3
    EVENT_NAMES = ["LOCAL_OPENED", "LOCAL_CLOSED",
                   "REMOTE_OPENED", "REMOTE_CLOSED"]

    # Endpoint Finite State Machine:
    # Indexed by current state, each entry is indexed by the event received and
    # returns a tuple of (next-state, action).  If there is no entry for a
    # given event, _ep_error() is invoked and the endpoint moves to the
    # terminal STATE_ERROR state.
    _FSM = {}
    _FSM[STATE_UNINIT] = {
        LOCAL_OPENED: (STATE_PENDING, None),
        REMOTE_OPENED: (STATE_REQUESTED, lambda s: s._ep_requested())
    }
    _FSM[STATE_PENDING] = {
        LOCAL_CLOSED: (STATE_CANCELLED, None),
        REMOTE_OPENED: (STATE_ACTIVE, lambda s: s._ep_active())
    }
    _FSM[STATE_REQUESTED] = {
        LOCAL_OPENED: (STATE_ACTIVE, lambda s: s._ep_active()),
        REMOTE_CLOSED: (STATE_ABANDONED, None)
    }
    _FSM[STATE_CANCELLED] = {
        REMOTE_OPENED: (STATE_CLOSING, None)
    }
    _FSM[STATE_ABANDONED] = {
        LOCAL_OPENED: (STATE_NEED_CLOSE, lambda s: s._ep_need_close()),
        LOCAL_CLOSED: (STATE_CLOSED, lambda s: s._ep_closed())
    }
    _FSM[STATE_ACTIVE] = {
        LOCAL_CLOSED: (STATE_CLOSING, None),
        REMOTE_CLOSED: (STATE_NEED_CLOSE, lambda s: s._ep_need_close())
    }
    _FSM[STATE_NEED_CLOSE] = {
        LOCAL_CLOSED: (STATE_CLOSED, lambda s: s._ep_closed())
    }
    _FSM[STATE_CLOSING] = {
        REMOTE_CLOSED: (STATE_CLOSED, lambda s: s._ep_closed())
    }
    _FSM[STATE_CLOSED] = {
        REMOTE_CLOSED: (STATE_CLOSED, None)
    }
    _FSM[STATE_ERROR] = {  # terminal state
        LOCAL_OPENED: (STATE_ERROR, None),
        LOCAL_CLOSED: (STATE_ERROR, None),
        REMOTE_OPENED: (STATE_ERROR, None),
        REMOTE_CLOSED: (STATE_ERROR, None)
    }

    def __init__(self, name):
        self._name = name
        self._state = Endpoint.STATE_UNINIT
        if (_PROTON_VERSION < (0, 8)):
            # The old proton event model did not generate specific endpoint
            # events.  Rather it simply indicated local or remote state change
            # occured without giving the value of the state (opened/closed).
            # Map these events to open and close events, assuming the Proton
            # endpoint state transitions are fixed to the following sequence:
            # UNINIT --> ACTIVE --> CLOSED
            self._remote_events = [Endpoint.REMOTE_OPENED,
                                   Endpoint.REMOTE_CLOSED]
            self._local_events = [Endpoint.LOCAL_OPENED,
                                  Endpoint.LOCAL_CLOSED]

    def _process_endpoint_event(self, event):
        """Called when the Proton Engine generates an endpoint state change
        event.
        """
        LOG.debug("Endpoint %s event: %s",
                  self._name, Endpoint.EVENT_NAMES[event])
        state_fsm = Endpoint._FSM[self._state]
        entry = state_fsm.get(event)
        if not entry:
            # protocol error: invalid event for current state
            old_state = self._state
            self._state = Endpoint.STATE_ERROR
            self._ep_error("invalid event=%s in state=%s" %
                           (Endpoint.EVENT_NAMES[event],
                            Endpoint.STATE_NAMES[old_state]))
            return

        LOG.debug("Endpoint %s Old State: %s New State: %s",
                  self._name,
                  Endpoint.STATE_NAMES[self._state],
                  Endpoint.STATE_NAMES[entry[0]])
        self._state = entry[0]
        if entry[1]:
            entry[1](self)

    if (_PROTON_VERSION < (0, 8)):
        def _process_remote_state(self):
            """Call when remote endpoint state changes."""
            try:
                event = self._remote_events.pop(0)
                self._process_endpoint_event(event)
            except IndexError:
                LOG.debug("Endpoint %s: ignoring unexpected remote event",
                          self._name)

        def _process_local_state(self):
            """Call when local endpoint state changes."""
            try:
                event = self._local_events.pop(0)
                self._process_endpoint_event(event)
            except IndexError:
                LOG.debug("Endpoint %s: ignoring unexpected local event",
                          self._name)

    @property
    def _endpoint_state(self):
        """Returns the current endpoint state."""
        raise NotImplementedError("Must Override")

    # state entry actions - overridden by endpoint subclass:

    def _ep_requested(self):
        """Remote has activated a new endpoint."""
        LOG.debug("endpoint_requested - ignored")

    def _ep_active(self):
        """Both ends of the Endpoint have become active."""
        LOG.debug("endpoint_active - ignored")

    def _ep_need_close(self):
        """The remote has closed its end of the endpoint."""
        LOG.debug("endpoint_need_close - ignored")

    def _ep_closed(self):
        """Both ends of the endpoint have closed."""
        LOG.debug("endpoint_closed - ignored")

    def _ep_error(self, error):
        """Unanticipated/illegal state change."""
        LOG.error("Endpoint state error: endpoint=%s, error=%s",
                  self._name, error)
