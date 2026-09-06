test tcPingPong [main = PingPongTest]:
  assert NoOverwrite, NoDuplicateConsume, NoStaleGeneration, OrderedStreamWatermarks, NoFalseRejectedBeforeExecute, NoUnknownReplay, NoPrematureRelease, EventuallyDrained in { TransportModel, PingPongTest };

test tcQueueFull [main = QueueFullTest]:
  assert NoOverwrite, NoDuplicateConsume, NoStaleGeneration, OrderedStreamWatermarks, NoFalseRejectedBeforeExecute, NoUnknownReplay, NoPrematureRelease, EventuallyDrained in { TransportModel, QueueFullTest };

test tcTimeoutOwnership [main = TimeoutOwnershipTest]:
  assert NoOverwrite, NoDuplicateConsume, NoStaleGeneration, OrderedStreamWatermarks, NoFalseRejectedBeforeExecute, NoUnknownReplay, NoPrematureRelease, EventuallyDrained in { TransportModel, TimeoutOwnershipTest };

test tcCoalescedRequestDisposition [main = CoalescedRequestDispositionTest]:
  assert NoOverwrite, NoDuplicateConsume, NoStaleGeneration, OrderedStreamWatermarks, NoFalseRejectedBeforeExecute, NoUnknownReplay, NoPrematureRelease, EventuallyDrained in { TransportModel, CoalescedRequestDispositionTest };

test tcRestartGeneration [main = RestartGenerationTest]:
  assert NoOverwrite, NoDuplicateConsume, NoStaleGeneration, OrderedStreamWatermarks, NoFalseRejectedBeforeExecute, NoUnknownReplay, NoPrematureRelease, EventuallyDrained in { TransportModel, RestartGenerationTest };

test tcOneWay [main = OneWayTest]:
  assert NoOverwrite, NoDuplicateConsume, NoStaleGeneration, OrderedStreamWatermarks, NoFalseRejectedBeforeExecute, NoUnknownReplay, NoPrematureRelease, EventuallyDrained in { TransportModel, OneWayTest };

test tcTwoWay [main = TwoWayTest]:
  assert NoOverwrite, NoDuplicateConsume, NoStaleGeneration, OrderedStreamWatermarks, NoFalseRejectedBeforeExecute, NoUnknownReplay, NoPrematureRelease, EventuallyDrained in { TransportModel, TwoWayTest };

test tcCorruptDeliveredRecord [main = CorruptDeliveredRecordTest]:
  assert NoOverwrite, NoDuplicateConsume, NoStaleGeneration, OrderedStreamWatermarks, NoFalseRejectedBeforeExecute, NoUnknownReplay, NoPrematureRelease, EventuallyDrained in { TransportModel, CorruptDeliveredRecordTest };

test tcCrashAfterDelivery [main = CrashAfterDeliveryTest]:
  assert NoOverwrite, NoDuplicateConsume, NoStaleGeneration, OrderedStreamWatermarks, NoFalseRejectedBeforeExecute, NoUnknownReplay, NoPrematureRelease, EventuallyDrained in { TransportModel, CrashAfterDeliveryTest };

test tcStaleSubrange [main = StaleSubrangeTest]:
  assert NoOverwrite, NoDuplicateConsume, NoStaleGeneration, OrderedStreamWatermarks, NoFalseRejectedBeforeExecute, NoUnknownReplay, NoPrematureRelease, EventuallyDrained in { TransportModel, StaleSubrangeTest };

test tcUnsafeOverwrite [main = UnsafeOverwriteTest]:
  assert NoOverwrite, NoDuplicateConsume, NoStaleGeneration, OrderedStreamWatermarks, NoFalseRejectedBeforeExecute, NoUnknownReplay, NoPrematureRelease, EventuallyDrained in { TransportModel, UnsafeOverwriteTest };

test tcUnsafePartialReplay [main = UnsafePartialReplayTest]:
  assert NoOverwrite, NoDuplicateConsume, NoStaleGeneration, OrderedStreamWatermarks, NoFalseRejectedBeforeExecute, NoUnknownReplay, NoPrematureRelease, EventuallyDrained in { TransportModel, UnsafePartialReplayTest };

test tcUnsafeUnknownReplay [main = UnsafeUnknownReplayTest]:
  assert NoOverwrite, NoDuplicateConsume, NoStaleGeneration, OrderedStreamWatermarks, NoFalseRejectedBeforeExecute, NoUnknownReplay, NoPrematureRelease, EventuallyDrained in { TransportModel, UnsafeUnknownReplayTest };

test tcUnsafeLeaseRelease [main = UnsafeLeaseReleaseTest]:
  assert NoOverwrite, NoDuplicateConsume, NoStaleGeneration, OrderedStreamWatermarks, NoFalseRejectedBeforeExecute, NoUnknownReplay, NoPrematureRelease, EventuallyDrained in { TransportModel, UnsafeLeaseReleaseTest };

test tcUnsafeStaleHandle [main = UnsafeStaleHandleTest]:
  assert NoOverwrite, NoDuplicateConsume, NoStaleGeneration, OrderedStreamWatermarks, NoFalseRejectedBeforeExecute, NoUnknownReplay, NoPrematureRelease, EventuallyDrained in { TransportModel, UnsafeStaleHandleTest };
