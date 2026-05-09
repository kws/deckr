import assert from "node:assert/strict";
import test from "node:test";

import {
  HARDWARE_MESSAGES_LANE,
  actionProviderAddress,
  contextSubject,
  controllerAddress,
  decodeKeyToken,
  encodeKeyToken,
  endpointTarget,
  headersFor,
  payloadJsonString,
  presenceEndpointKey,
  settingsTargetKey,
  subjectFor,
  subscribeSubjectForLane,
  validateSubjectHint,
  buildMessage,
  BINDING_OUTPUT,
} from "../src/index.js";

test("encodes key tokens with Deckr's NATS-safe rules", () => {
  assert.equal(encodeKeyToken("deck_1"), "deck_1");
  assert.equal(encodeKeyToken("b64_native"), "b64_YjY0X25hdGl2ZQ");
  assert.equal(encodeKeyToken("deck:one"), "b64_ZGVjazpvbmU");
  assert.equal(decodeKeyToken("b64_ZGVjazpvbmU"), "deck:one");
});

test("builds canonical state and settings keys", () => {
  assert.equal(
    presenceEndpointKey({
      lane: "actions",
      endpoint: "action_provider:elgato.com.example.plugin",
    }),
    "presence.endpoint.actions.action_provider.b64_ZWxnYXRvLmNvbS5leGFtcGxlLnBsdWdpbg",
  );
  assert.equal(
    settingsTargetKey({
      scope: "action_instance",
      controllerId: "controller-main",
      configId: "office-panel",
      providerInstanceId: "clock-main",
      providerId: "dev.deckr.clock",
      actionId: "dev.deckr.clock.time",
      actionInstanceId: "clock-instance-1",
      stableId: "clock",
    }),
    "settings.target.action_instance.controller-main.office-panel.clock-main.b64_ZGV2LmRlY2tyLmNsb2Nr.b64_ZGV2LmRlY2tyLmNsb2NrLnRpbWU.clock-instance-1.1.clock",
  );
});

test("maps Deckr messages to canonical NATS subjects, headers, and payloads", () => {
  const message = buildMessage({
    messageId: "message-1",
    createdAt: "2026-04-29T10:00:00Z",
    sender: actionProviderAddress("clock-main"),
    senderSessionId: "provider-session",
    recipient: endpointTarget(controllerAddress("controller-main")),
    recipientSessionId: "controller-session",
    messageType: BINDING_OUTPUT,
    body: { commandType: "clear" },
    subject: contextSubject("clock-context", {
      providerInstanceId: "clock-main",
      providerId: "dev.deckr.clock",
      bindingId: "binding-1",
      configId: "office-panel",
    }),
  });

  assert.equal(subjectFor(message), "deckr.lane.actions.action_provider.clock-main");
  assert.deepEqual(headersFor(message), {
    "Deckr-Message-Id": "message-1",
    "Deckr-Message-Type": "bindingOutput",
    "Deckr-Sender": "action_provider:clock-main",
    "Deckr-Sender-Session": "provider-session",
    "Deckr-Recipient": "controller:controller-main",
    "Deckr-Recipient-Session": "controller-session",
  });
  assert.equal(
    payloadJsonString(message),
    "{\"messageId\":\"message-1\",\"protocolVersion\":\"1\",\"schemaVersion\":\"1\",\"lane\":\"actions\",\"messageType\":\"bindingOutput\",\"sender\":\"action_provider:clock-main\",\"senderSessionId\":\"provider-session\",\"recipient\":{\"targetType\":\"endpoint\",\"endpoint\":\"controller:controller-main\"},\"recipientSessionId\":\"controller-session\",\"subject\":{\"kind\":\"context\",\"identifiers\":{\"contextId\":\"clock-context\",\"providerInstanceId\":\"clock-main\",\"providerId\":\"dev.deckr.clock\",\"bindingId\":\"binding-1\",\"configId\":\"office-panel\"}},\"createdAt\":\"2026-04-29T10:00:00Z\",\"body\":{\"commandType\":\"clear\"}}",
  );
  assert.doesNotThrow(() => validateSubjectHint(subjectFor(message), message));
});

test("builds lane subscription wildcard subjects", () => {
  assert.equal(
    subscribeSubjectForLane(HARDWARE_MESSAGES_LANE),
    "deckr.lane.hardware_messages.>",
  );
  assert.equal(
    subscribeSubjectForLane("owner/custom lane"),
    "deckr.lane.b64_b3duZXIvY3VzdG9tIGxhbmU.>",
  );
});
