#!/usr/bin/env python
"""This module contains tests for user API handlers."""

from grr.lib import flags

from grr.lib import rdfvalue
from grr.lib import utils

from grr.lib.rdfvalues import flows as rdf_flows
from grr.server.grr_response_server import access_control
from grr.server.grr_response_server import aff4
from grr.server.grr_response_server import data_store
from grr.server.grr_response_server import email_alerts
from grr.server.grr_response_server.aff4_objects import cronjobs as aff4_cronjobs
from grr.server.grr_response_server.aff4_objects import users as aff4_users
from grr.server.grr_response_server.flows.general import administrative
from grr.server.grr_response_server.gui import api_call_handler_base

from grr.server.grr_response_server.gui import api_test_lib
from grr.server.grr_response_server.gui.api_plugins import user as user_plugin
from grr.server.grr_response_server.hunts import implementation

from grr.server.grr_response_server.hunts import standard

from grr.test_lib import acl_test_lib
from grr.test_lib import db_test_lib
from grr.test_lib import hunt_test_lib
from grr.test_lib import test_lib


class ApiNotificationTest(api_test_lib.ApiCallHandlerTest):
  """Tests for ApiNotification class."""

  def setUp(self):
    super(ApiNotificationTest, self).setUp()
    self.client_id = self.SetupClient(0)

  def InitFromObj_(self, notification_type, subject, message=None):
    self._SendNotification(
        notification_type=notification_type,
        subject=subject,
        message=message,
        client_id=self.client_id)

    user_record = aff4.FACTORY.Open(
        aff4.ROOT_URN.Add("users").Add(self.token.username), token=self.token)
    pending_notifications = user_record.Get(
        user_record.Schema.PENDING_NOTIFICATIONS)

    result = user_plugin.ApiNotification().InitFromNotification(
        pending_notifications.Pop())
    aff4.FACTORY.Delete(
        aff4.ROOT_URN.Add("users").Add(self.token.username), token=self.token)
    return result

  def testDiscoveryNotificationIsParsedCorrectly(self):
    n = self.InitFromObj_("Discovery", self.client_id)
    self.assertEqual(n.reference.type, "DISCOVERY")
    self.assertEqual(n.reference.discovery.client_id, self.client_id)

    n = self.InitFromObj_("ViewObject", self.client_id)
    self.assertEqual(n.reference.type, "DISCOVERY")
    self.assertEqual(n.reference.discovery.client_id, self.client_id)

  def testHuntNotificationIsParsedCorrectly(self):
    n = self.InitFromObj_("ViewObject", "aff4:/hunts/H:123456")
    self.assertEqual(n.reference.type, "HUNT")
    self.assertEqual(n.reference.hunt.hunt_urn, "aff4:/hunts/H:123456")

  def testCronNotificationIsParsedCorrectly(self):
    n = self.InitFromObj_("ViewObject", "aff4:/cron/FooBar")
    self.assertEqual(n.reference.type, "CRON")
    self.assertEqual(n.reference.cron.cron_job_urn, "aff4:/cron/FooBar")

  def testFlowNotificationIsParsedCorrectly(self):
    n = self.InitFromObj_("ViewObject", self.client_id.Add("flows/F:123456"))
    self.assertEqual(n.reference.type, "FLOW")
    self.assertEqual(n.reference.flow.client_id, self.client_id)
    self.assertEqual(n.reference.flow.flow_id, "F:123456")

    n = self.InitFromObj_("FlowStatus", self.client_id)
    self.assertEqual(n.reference.type, "FLOW")
    self.assertEqual(n.reference.flow.client_id, self.client_id)
    # Source flow id is autogenerated by self._SendNotification, so we just
    # check that reference.flow.flow_id is set.
    self.assertTrue(str(n.reference.flow.flow_id))

  def testVfsNotificationIsParsedCorrectly(self):
    n = self.InitFromObj_("ViewObject", self.client_id.Add("fs/os/foo/bar"))
    self.assertEqual(n.reference.type, "VFS")
    self.assertEqual(n.reference.vfs.client_id, self.client_id)
    self.assertEqual(n.reference.vfs.vfs_path, "fs/os/foo/bar")

  def testClientApprovalNotificationIsParsedCorrectly(self):
    n = self.InitFromObj_(
        "GrantAccess", "aff4:/ACL/%s/%s/foo-bar" % (self.client_id.Basename(),
                                                    self.token.username))
    self.assertEqual(n.reference.type, "CLIENT_APPROVAL")
    self.assertEqual(n.reference.client_approval.client_id, self.client_id)
    self.assertEqual(n.reference.client_approval.username, self.token.username)
    self.assertEqual(n.reference.client_approval.approval_id, "foo-bar")

  def testHuntApprovalNotificationIsParsedCorrectly(self):
    n = self.InitFromObj_(
        "GrantAccess",
        "aff4:/ACL/hunts/H:123456/%s/foo-bar" % self.token.username)
    self.assertEqual(n.reference.type, "HUNT_APPROVAL")
    self.assertEqual(n.reference.hunt_approval.hunt_id, "H:123456")
    self.assertEqual(n.reference.hunt_approval.username, self.token.username)
    self.assertEqual(n.reference.hunt_approval.approval_id, "foo-bar")

  def testCronJobApprovalNotificationIsParsedCorrectly(self):
    n = self.InitFromObj_(
        "GrantAccess", "aff4:/ACL/cron/FooBar/%s/foo-bar" % self.token.username)
    self.assertEqual(n.reference.type, "CRON_JOB_APPROVAL")
    self.assertEqual(n.reference.cron_job_approval.cron_job_id, "FooBar")
    self.assertEqual(n.reference.cron_job_approval.username,
                     self.token.username)
    self.assertEqual(n.reference.cron_job_approval.approval_id, "foo-bar")

  def testUnknownNotificationIsParsedCorrectly(self):
    n = self.InitFromObj_("ViewObject", self.client_id.Add("foo/bar"))
    self.assertEqual(n.reference.type, "UNKNOWN")
    self.assertEqual(n.reference.unknown.subject_urn,
                     self.client_id.Add("foo/bar"))

    n = self.InitFromObj_("FlowStatus", "foo/bar")
    self.assertEqual(n.reference.type, "UNKNOWN")
    self.assertEqual(n.reference.unknown.subject_urn, "foo/bar")

  def testNotificationWithoutSubject(self):
    notification = rdf_flows.Notification(type="ViewObject")

    result = user_plugin.ApiNotification().InitFromNotification(notification)
    self.assertEqual(result.reference.type, "UNKNOWN")


class ApiCreateApprovalHandlerTestMixin(acl_test_lib.AclTestMixin):
  """Base class for tests testing Create*ApprovalHandlers."""

  def SetUpApprovalTest(self):
    self.CreateUser("test")
    self.CreateUser("approver")

    self.handler = None
    self.args = None

  def ReadApproval(self, approval_id):
    raise NotImplementedError()

  def testCreatesAnApprovalWithGivenAttributes(self):
    approval_id = self.handler.Handle(self.args, token=self.token).id
    approval_obj = self.ReadApproval(approval_id)

    self.assertEqual(approval_obj.reason, self.token.reason)
    self.assertEqual(approval_obj.approvers, [self.token.username])
    self.assertEqual(approval_obj.email_cc_addresses, ["test@example.com"])

  def testApproversFromArgsAreIgnored(self):
    # It shouldn't be possible to specify list of approvers when creating
    # an approval. List of approvers contains names of GRR users who
    # approved the approval.
    self.args.approval.approvers = [self.token.username, "approver"]

    approval_id = self.handler.Handle(self.args, token=self.token).id
    approval_obj = self.ReadApproval(approval_id)

    self.assertEqual(approval_obj.approvers, [self.token.username])

  def testRaisesOnEmptyReason(self):
    self.args.approval.reason = ""

    with self.assertRaises(ValueError):
      self.handler.Handle(self.args, token=self.token)

  @db_test_lib.LegacyDataStoreOnly
  def testNotifiesGrrUsers(self):
    self.handler.Handle(self.args, token=self.token)

    fd = aff4.FACTORY.Open(
        "aff4:/users/approver", aff4_type=aff4_users.GRRUser, token=self.token)
    notifications = fd.ShowNotifications(reset=False)

    self.assertEqual(len(notifications), 1)

  def testSendsEmailsToGrrUsersAndCcAddresses(self):
    addresses = []

    def SendEmailStub(to_user,
                      from_user,
                      unused_subject,
                      unused_message,
                      cc_addresses=None,
                      **unused_kwargs):
      addresses.append((to_user, from_user, cc_addresses))

    with utils.Stubber(email_alerts.EMAIL_ALERTER, "SendEmail", SendEmailStub):
      self.handler.Handle(self.args, token=self.token)

    self.assertEqual(len(addresses), 1)
    self.assertEqual(addresses[0],
                     ("approver", self.token.username, "test@example.com"))


@db_test_lib.DualDBTest
class ApiGetClientApprovalHandlerTest(acl_test_lib.AclTestMixin,
                                      api_test_lib.ApiCallHandlerTest):
  """Test for ApiGetClientApprovalHandler."""

  def setUp(self):
    super(ApiGetClientApprovalHandlerTest, self).setUp()
    self.client_id = self.SetupClient(0)
    self.handler = user_plugin.ApiGetClientApprovalHandler()

  def testRendersRequestedClientApproval(self):
    approval_id = self.RequestClientApproval(
        self.client_id.Basename(),
        requestor=self.token.username,
        reason="blah",
        approver="approver",
        email_cc_address="test@example.com")

    args = user_plugin.ApiGetClientApprovalArgs(
        client_id=self.client_id,
        approval_id=approval_id,
        username=self.token.username)
    result = self.handler.Handle(args, token=self.token)

    self.assertEqual(result.subject.client_id, self.client_id)
    self.assertEqual(result.reason, "blah")
    self.assertEqual(result.is_valid, False)
    self.assertEqual(result.is_valid_message,
                     "Need at least 1 additional approver for access.")

    self.assertEqual(result.notified_users, ["approver"])
    self.assertEqual(result.email_cc_addresses, ["test@example.com"])

    # Every approval is self-approved by default.
    self.assertEqual(result.approvers, [self.token.username])

  def testIncludesApproversInResultWhenApprovalIsGranted(self):
    approval_id = self.RequestAndGrantClientApproval(
        self.client_id.Basename(),
        reason="blah",
        approver="approver",
        requestor=self.token.username)

    args = user_plugin.ApiGetClientApprovalArgs(
        client_id=self.client_id,
        approval_id=approval_id,
        username=self.token.username)
    result = self.handler.Handle(args, token=self.token)

    self.assertTrue(result.is_valid)
    self.assertEqual(
        sorted(result.approvers), sorted([self.token.username, "approver"]))

  def testRaisesWhenApprovalIsNotFound(self):
    args = user_plugin.ApiGetClientApprovalArgs(
        client_id=self.client_id,
        approval_id="approval:112233",
        username=self.token.username)

    with self.assertRaises(api_call_handler_base.ResourceNotFoundError):
      self.handler.Handle(args, token=self.token)


@db_test_lib.DualDBTest
class ApiCreateClientApprovalHandlerTest(api_test_lib.ApiCallHandlerTest,
                                         ApiCreateApprovalHandlerTestMixin):
  """Test for ApiCreateClientApprovalHandler."""

  def ReadApproval(self, approval_id):
    approvals = self.ListClientApprovals(requestor=self.token.username)
    self.assertEqual(len(approvals), 1)
    self.assertEqual(approvals[0].id, approval_id)
    return approvals[0]

  def setUp(self):
    super(ApiCreateClientApprovalHandlerTest, self).setUp()

    self.SetUpApprovalTest()

    self.subject_urn = client_id = self.SetupClient(0)

    self.handler = user_plugin.ApiCreateClientApprovalHandler()

    self.args = user_plugin.ApiCreateClientApprovalArgs(client_id=client_id)
    self.args.approval.reason = self.token.reason
    self.args.approval.notified_users = ["approver"]
    self.args.approval.email_cc_addresses = ["test@example.com"]

  def testKeepAliveFlowIsStartedWhenFlagIsSet(self):
    self.args.keep_client_alive = True

    self.handler.Handle(self.args, self.token)
    flows = aff4.FACTORY.Open(
        self.subject_urn.Add("flows"), token=self.token).OpenChildren()
    keep_alive_flow = [
        f for f in flows if f.__class__ == administrative.KeepAlive
    ]
    self.assertEqual(len(keep_alive_flow), 1)


@db_test_lib.DualDBTest
class ApiListClientApprovalsHandlerTest(api_test_lib.ApiCallHandlerTest,
                                        acl_test_lib.AclTestMixin):
  """Test for ApiListApprovalsHandler."""

  CLIENT_COUNT = 5

  def setUp(self):
    super(ApiListClientApprovalsHandlerTest, self).setUp()
    self.handler = user_plugin.ApiListClientApprovalsHandler()
    self.client_ids = self.SetupClients(self.CLIENT_COUNT)

  def _RequestClientApprovals(self):
    approval_ids = []
    for client_id in self.client_ids:
      approval_ids.append(self.RequestClientApproval(client_id.Basename()))
    return approval_ids

  def testRendersRequestedClientApprovals(self):
    self._RequestClientApprovals()

    args = user_plugin.ApiListClientApprovalsArgs()
    result = self.handler.Handle(args, token=self.token)

    # All approvals should be returned.
    self.assertEqual(len(result.items), self.CLIENT_COUNT)

  def testFiltersApprovalsByClientId(self):
    client_id = self.client_ids[0]

    self._RequestClientApprovals()

    # Get approvals for a specific client. There should be exactly one.
    args = user_plugin.ApiListClientApprovalsArgs(client_id=client_id)
    result = self.handler.Handle(args, token=self.token)

    self.assertEqual(len(result.items), 1)
    self.assertEqual(result.items[0].subject.client_id, client_id)

  def testFiltersApprovalsByInvalidState(self):
    approval_ids = self._RequestClientApprovals()

    # We only requested approvals so far, so all of them should be invalid.
    args = user_plugin.ApiListClientApprovalsArgs(
        state=user_plugin.ApiListClientApprovalsArgs.State.INVALID)
    result = self.handler.Handle(args, token=self.token)

    self.assertEqual(len(result.items), self.CLIENT_COUNT)

    # Grant access to one client. Now all but one should be invalid.
    self.GrantClientApproval(
        self.client_ids[0],
        requestor=self.token.username,
        approval_id=approval_ids[0])
    result = self.handler.Handle(args, token=self.token)
    self.assertEqual(len(result.items), self.CLIENT_COUNT - 1)

  def testFiltersApprovalsByValidState(self):
    approval_ids = self._RequestClientApprovals()

    # We only requested approvals so far, so none of them is valid.
    args = user_plugin.ApiListClientApprovalsArgs(
        state=user_plugin.ApiListClientApprovalsArgs.State.VALID)
    result = self.handler.Handle(args, token=self.token)

    # We do not have any approved approvals yet.
    self.assertEqual(len(result.items), 0)

    # Grant access to one client. Now exactly one approval should be valid.
    self.GrantClientApproval(
        self.client_ids[0].Basename(),
        requestor=self.token.username,
        approval_id=approval_ids[0])
    result = self.handler.Handle(args, token=self.token)
    self.assertEqual(len(result.items), 1)
    self.assertEqual(result.items[0].subject.client_id, self.client_ids[0])

  def testFiltersApprovalsByClientIdAndState(self):
    client_id = self.client_ids[0]

    approval_ids = self._RequestClientApprovals()

    # Grant approval to a certain client.
    self.GrantClientApproval(
        client_id.Basename(),
        requestor=self.token.username,
        approval_id=approval_ids[0])

    args = user_plugin.ApiListClientApprovalsArgs(
        client_id=client_id,
        state=user_plugin.ApiListClientApprovalsArgs.State.VALID)
    result = self.handler.Handle(args, token=self.token)

    # We have a valid approval for the requested client.
    self.assertEqual(len(result.items), 1)

    args.state = user_plugin.ApiListClientApprovalsArgs.State.INVALID
    result = self.handler.Handle(args, token=self.token)

    # However, we do not have any invalid approvals for the client.
    self.assertEqual(len(result.items), 0)

  def testFilterConsidersOffsetAndCount(self):
    client_id = self.client_ids[0]

    # Create five approval requests without granting them.
    for i in range(10):
      with test_lib.FakeTime(42 + i):
        self.RequestClientApproval(
            client_id.Basename(), reason="Request reason %d" % i)

    args = user_plugin.ApiListClientApprovalsArgs(
        client_id=client_id, offset=0, count=5)
    result = self.handler.Handle(args, token=self.token)

    # Approvals are returned newest to oldest, so the first five approvals
    # have reason 9 to 5.
    self.assertEqual(len(result.items), 5)
    for item, i in zip(result.items, reversed(range(6, 10))):
      self.assertEqual(item.reason, "Request reason %d" % i)

    # When no count is specified, take all items from offset to the end.
    args = user_plugin.ApiListClientApprovalsArgs(client_id=client_id, offset=7)
    result = self.handler.Handle(args, token=self.token)

    self.assertEqual(len(result.items), 3)
    for item, i in zip(result.items, reversed(range(0, 3))):
      self.assertEqual(item.reason, "Request reason %d" % i)


@db_test_lib.DualDBTest
class ApiCreateHuntApprovalHandlerTest(api_test_lib.ApiCallHandlerTest,
                                       ApiCreateApprovalHandlerTestMixin,
                                       hunt_test_lib.StandardHuntTestMixin):
  """Test for ApiCreateHuntApprovalHandler."""

  def ReadApproval(self, approval_id):
    approvals = self.ListHuntApprovals(requestor=self.token.username)
    self.assertEqual(len(approvals), 1)
    self.assertEqual(approvals[0].id, approval_id)
    return approvals[0]

  def setUp(self):
    super(ApiCreateHuntApprovalHandlerTest, self).setUp()

    self.SetUpApprovalTest()

    with self.CreateHunt(description="foo") as hunt_obj:
      hunt_id = hunt_obj.urn.Basename()

    self.handler = user_plugin.ApiCreateHuntApprovalHandler()

    self.args = user_plugin.ApiCreateHuntApprovalArgs(hunt_id=hunt_id)
    self.args.approval.reason = self.token.reason
    self.args.approval.notified_users = ["approver"]
    self.args.approval.email_cc_addresses = ["test@example.com"]


@db_test_lib.DualDBTest
class ApiListHuntApprovalsHandlerTest(acl_test_lib.AclTestMixin,
                                      api_test_lib.ApiCallHandlerTest):
  """Test for ApiListHuntApprovalsHandler."""

  def setUp(self):
    super(ApiListHuntApprovalsHandlerTest, self).setUp()
    self.handler = user_plugin.ApiListHuntApprovalsHandler()

  def testRendersRequestedHuntAppoval(self):
    with implementation.GRRHunt.StartHunt(
        hunt_name=standard.SampleHunt.__name__, token=self.token) as hunt:
      pass

    self.RequestHuntApproval(
        hunt.urn.Basename(),
        reason=self.token.reason,
        approver="approver",
        requestor=self.token.username)

    args = user_plugin.ApiListHuntApprovalsArgs()
    result = self.handler.Handle(args, token=self.token)

    self.assertEqual(len(result.items), 1)


@db_test_lib.DualDBTest
class ApiCreateCronJobApprovalHandlerTest(
    ApiCreateApprovalHandlerTestMixin,
    api_test_lib.ApiCallHandlerTest,
):
  """Test for ApiCreateCronJobApprovalHandler."""

  def ReadApproval(self, approval_id):
    approvals = self.ListCronJobApprovals(requestor=self.token.username)
    self.assertEqual(len(approvals), 1)
    self.assertEqual(approvals[0].id, approval_id)
    return approvals[0]

  def setUp(self):
    super(ApiCreateCronJobApprovalHandlerTest, self).setUp()

    self.SetUpApprovalTest()

    cron_manager = aff4_cronjobs.CronManager()
    cron_args = aff4_cronjobs.CreateCronJobFlowArgs(
        periodicity="1d", allow_overruns=False)
    cron_urn = cron_manager.ScheduleFlow(cron_args=cron_args, token=self.token)
    cron_id = cron_urn.Basename()

    self.handler = user_plugin.ApiCreateCronJobApprovalHandler()

    self.args = user_plugin.ApiCreateCronJobApprovalArgs(cron_job_id=cron_id)
    self.args.approval.reason = self.token.reason
    self.args.approval.notified_users = ["approver"]
    self.args.approval.email_cc_addresses = ["test@example.com"]


@db_test_lib.DualDBTest
class ApiListCronJobApprovalsHandlerTest(acl_test_lib.AclTestMixin,
                                         api_test_lib.ApiCallHandlerTest):
  """Test for ApiListCronJobApprovalsHandler."""

  def setUp(self):
    super(ApiListCronJobApprovalsHandlerTest, self).setUp()
    self.handler = user_plugin.ApiListCronJobApprovalsHandler()

  def testRendersRequestedCronJobApproval(self):
    cron_manager = aff4_cronjobs.CronManager()
    cron_args = aff4_cronjobs.CreateCronJobFlowArgs(
        periodicity="1d", allow_overruns=False)
    cron_job_urn = cron_manager.ScheduleFlow(
        cron_args=cron_args, token=self.token)

    self.RequestCronJobApproval(
        cron_job_urn.Basename(),
        reason=self.token.reason,
        approver="approver",
        requestor=self.token.username)

    args = user_plugin.ApiListCronJobApprovalsArgs()
    result = self.handler.Handle(args, token=self.token)

    self.assertEqual(len(result.items), 1)


class ApiGetOwnGrrUserHandlerTest(api_test_lib.ApiCallHandlerTest):
  """Test for ApiGetUserSettingsHandler."""

  def setUp(self):
    super(ApiGetOwnGrrUserHandlerTest, self).setUp()
    self.handler = user_plugin.ApiGetOwnGrrUserHandler()

  def testRendersObjectForNonExistingUser(self):
    result = self.handler.Handle(
        None, token=access_control.ACLToken(username="foo"))
    self.assertEqual(result.username, "foo")

  def testRendersSettingsForUserCorrespondingToToken(self):
    with aff4.FACTORY.Create(
        aff4.ROOT_URN.Add("users").Add("foo"),
        aff4_type=aff4_users.GRRUser,
        mode="w",
        token=self.token) as user_fd:
      user_fd.Set(user_fd.Schema.GUI_SETTINGS,
                  aff4_users.GUISettings(
                      mode="ADVANCED",
                      canary_mode=True,
                  ))

    result = self.handler.Handle(
        None, token=access_control.ACLToken(username="foo"))
    self.assertEqual(result.settings.mode, "ADVANCED")
    self.assertEqual(result.settings.canary_mode, True)

  def testRendersTraitsPassedInConstructor(self):
    result = self.handler.Handle(
        None, token=access_control.ACLToken(username="foo"))
    self.assertFalse(result.interface_traits.create_hunt_action_enabled)

    handler = user_plugin.ApiGetOwnGrrUserHandler(
        interface_traits=user_plugin.ApiGrrUserInterfaceTraits(
            create_hunt_action_enabled=True))
    result = handler.Handle(None, token=access_control.ACLToken(username="foo"))
    self.assertTrue(result.interface_traits.create_hunt_action_enabled)


class ApiUpdateGrrUserHandlerTest(api_test_lib.ApiCallHandlerTest):
  """Tests for ApiUpdateUserSettingsHandler."""

  def setUp(self):
    super(ApiUpdateGrrUserHandlerTest, self).setUp()
    self.handler = user_plugin.ApiUpdateGrrUserHandler()

  def testRaisesIfUsernameSetInRequest(self):
    user = user_plugin.ApiGrrUser(username="foo")
    with self.assertRaises(ValueError):
      self.handler.Handle(user, token=access_control.ACLToken(username="foo"))

    user = user_plugin.ApiGrrUser(username="bar")
    with self.assertRaises(ValueError):
      self.handler.Handle(user, token=access_control.ACLToken(username="foo"))

  def testRaisesIfTraitsSetInRequest(self):
    user = user_plugin.ApiGrrUser(
        interface_traits=user_plugin.ApiGrrUserInterfaceTraits())
    with self.assertRaises(ValueError):
      self.handler.Handle(user, token=access_control.ACLToken(username="foo"))

  def testSetsSettingsForUserCorrespondingToToken(self):
    settings = aff4_users.GUISettings(mode="ADVANCED", canary_mode=True)
    user = user_plugin.ApiGrrUser(settings=settings)

    self.handler.Handle(user, token=access_control.ACLToken(username="foo"))

    # Check that settings for user "foo" were applied.
    fd = aff4.FACTORY.Open("aff4:/users/foo", token=self.token)
    self.assertEqual(fd.Get(fd.Schema.GUI_SETTINGS), settings)

    # Check that settings were applied in relational db.
    u = data_store.REL_DB.ReadGRRUser("foo")
    self.assertEqual(settings.mode, u.ui_mode)
    self.assertEqual(settings.canary_mode, u.canary_mode)


class ApiDeletePendingUserNotificationHandlerTest(
    api_test_lib.ApiCallHandlerTest):
  """Test for ApiDeletePendingUserNotificationHandler."""

  TIME_0 = rdfvalue.RDFDatetime(42 * rdfvalue.MICROSECONDS)
  TIME_1 = TIME_0 + rdfvalue.Duration("1d")
  TIME_2 = TIME_1 + rdfvalue.Duration("1d")

  def setUp(self):
    super(ApiDeletePendingUserNotificationHandlerTest, self).setUp()
    self.handler = user_plugin.ApiDeletePendingUserNotificationHandler()
    self.client_id = self.SetupClient(0)

    with test_lib.FakeTime(self.TIME_0):
      self._SendNotification(
          notification_type="Discovery",
          subject=str(self.client_id),
          message="<some message>",
          client_id=self.client_id)

      self._SendNotification(
          notification_type="Discovery",
          subject=str(self.client_id),
          message="<some message with identical time>",
          client_id=self.client_id)

    with test_lib.FakeTime(self.TIME_1):
      self._SendNotification(
          notification_type="ViewObject",
          subject=str(self.client_id),
          message="<some other message>",
          client_id=self.client_id)

  def _GetNotifications(self):
    user_record = aff4.FACTORY.Create(
        aff4.ROOT_URN.Add("users").Add(self.token.username),
        aff4_type=aff4_users.GRRUser,
        mode="r",
        token=self.token)

    pending = user_record.Get(user_record.Schema.PENDING_NOTIFICATIONS)
    shown = user_record.Get(user_record.Schema.SHOWN_NOTIFICATIONS)
    return (pending, shown)

  def testDeletesFromPendingAndAddsToShown(self):
    # Check that there are three pending notifications and no shown ones yet.
    (pending, shown) = self._GetNotifications()
    self.assertEqual(len(pending), 3)
    self.assertEqual(len(shown), 0)

    # Delete a pending notification.
    args = user_plugin.ApiDeletePendingUserNotificationArgs(
        timestamp=self.TIME_1)
    self.handler.Handle(args, token=self.token)

    # After the deletion, two notifications should be pending and one shown.
    (pending, shown) = self._GetNotifications()
    self.assertEqual(len(pending), 2)
    self.assertEqual(len(shown), 1)
    self.assertTrue("<some other message>" in shown[0].message)
    self.assertEqual(shown[0].timestamp, self.TIME_1)

  def testRaisesOnDeletingMultipleNotifications(self):
    # Check that there are three pending notifications and no shown ones yet.
    (pending, shown) = self._GetNotifications()
    self.assertEqual(len(pending), 3)
    self.assertEqual(len(shown), 0)

    # Delete all pending notifications on TIME_0.
    args = user_plugin.ApiDeletePendingUserNotificationArgs(
        timestamp=self.TIME_0)
    with self.assertRaises(aff4_users.UniqueKeyError):
      self.handler.Handle(args, token=self.token)

    # Check that the notifications were not changed in the process.
    (pending, shown) = self._GetNotifications()
    self.assertEqual(len(pending), 3)
    self.assertEqual(len(shown), 0)

  def testUnknownTimestampIsIgnored(self):
    # Check that there are three pending notifications and no shown ones yet.
    (pending, shown) = self._GetNotifications()
    self.assertEqual(len(pending), 3)
    self.assertEqual(len(shown), 0)

    # A timestamp not matching any pending notifications does not change any of
    # the collections.
    args = user_plugin.ApiDeletePendingUserNotificationArgs(
        timestamp=self.TIME_2)
    self.handler.Handle(args, token=self.token)

    # We should still have the same number of pending and shown notifications.
    (pending, shown) = self._GetNotifications()
    self.assertEqual(len(pending), 3)
    self.assertEqual(len(shown), 0)


class ApiDeletePendingGlobalNotificationHandlerTest(
    api_test_lib.ApiCallHandlerTest):
  """Test for ApiDeletePendingGlobalNotificationHandler."""

  def setUp(self):
    super(ApiDeletePendingGlobalNotificationHandlerTest, self).setUp()
    self.handler = user_plugin.ApiDeletePendingGlobalNotificationHandler()

    with aff4.FACTORY.Create(
        aff4_users.GlobalNotificationStorage.DEFAULT_PATH,
        aff4_type=aff4_users.GlobalNotificationStorage,
        mode="rw",
        token=self.token) as storage:
      storage.AddNotification(
          aff4_users.GlobalNotification(
              type=aff4_users.GlobalNotification.Type.ERROR,
              header="Oh no, we're doomed!",
              content="Houston, Houston, we have a prob...",
              link="http://www.google.com"))
      storage.AddNotification(
          aff4_users.GlobalNotification(
              type=aff4_users.GlobalNotification.Type.INFO,
              header="Nothing to worry about!",
              link="http://www.google.com"))

  def _GetGlobalNotifications(self):
    user_record = aff4.FACTORY.Create(
        aff4.ROOT_URN.Add("users").Add(self.token.username),
        aff4_type=aff4_users.GRRUser,
        mode="r",
        token=self.token)

    pending = user_record.GetPendingGlobalNotifications()
    shown = list(user_record.Get(user_record.Schema.SHOWN_GLOBAL_NOTIFICATIONS))
    return (pending, shown)

  def testDeletesFromPendingAndAddsToShown(self):
    # Check that there are two pending notifications and no shown ones yet.
    (pending, shown) = self._GetGlobalNotifications()
    self.assertEqual(len(pending), 2)
    self.assertEqual(len(shown), 0)

    # Delete one of the pending notifications.
    args = user_plugin.ApiDeletePendingGlobalNotificationArgs(
        type=aff4_users.GlobalNotification.Type.INFO)
    self.handler.Handle(args, token=self.token)

    # After the deletion, one notification should be pending and one shown.
    (pending, shown) = self._GetGlobalNotifications()
    self.assertEqual(len(pending), 1)
    self.assertEqual(len(shown), 1)
    self.assertEqual(pending[0].header, "Oh no, we're doomed!")
    self.assertEqual(shown[0].header, "Nothing to worry about!")

  def testRaisesOnDeletingNonExistingNotification(self):
    # Check that there are two pending notifications and no shown ones yet.
    (pending, shown) = self._GetGlobalNotifications()
    self.assertEqual(len(pending), 2)
    self.assertEqual(len(shown), 0)

    # Delete a non-existing pending notification.
    args = user_plugin.ApiDeletePendingGlobalNotificationArgs(
        type=aff4_users.GlobalNotification.Type.WARNING)
    with self.assertRaises(user_plugin.GlobalNotificationNotFoundError):
      self.handler.Handle(args, token=self.token)

    # Check that the notifications were not changed in the process.
    (pending, shown) = self._GetGlobalNotifications()
    self.assertEqual(len(pending), 2)
    self.assertEqual(len(shown), 0)


def main(argv):
  test_lib.main(argv)


if __name__ == "__main__":
  flags.StartMain(main)
