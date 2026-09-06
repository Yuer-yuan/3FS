machine PingPongTest {
  start state Init {
    entry { new TransportModel((scenario = 0, unsafe = 0)); }
  }
}

machine QueueFullTest {
  start state Init {
    entry { new TransportModel((scenario = 1, unsafe = 0)); }
  }
}

machine TimeoutOwnershipTest {
  start state Init {
    entry { new TransportModel((scenario = 2, unsafe = 0)); }
  }
}

machine CoalescedRequestDispositionTest {
  start state Init {
    entry { new TransportModel((scenario = 3, unsafe = 0)); }
  }
}

machine RestartGenerationTest {
  start state Init {
    entry { new TransportModel((scenario = 4, unsafe = 0)); }
  }
}

machine OneWayTest {
  start state Init {
    entry { new TransportModel((scenario = 5, unsafe = 0)); }
  }
}

machine TwoWayTest {
  start state Init {
    entry { new TransportModel((scenario = 6, unsafe = 0)); }
  }
}

machine CorruptDeliveredRecordTest {
  start state Init {
    entry { new TransportModel((scenario = 7, unsafe = 0)); }
  }
}

machine CrashAfterDeliveryTest {
  start state Init {
    entry { new TransportModel((scenario = 8, unsafe = 0)); }
  }
}

machine StaleSubrangeTest {
  start state Init {
    entry { new TransportModel((scenario = 9, unsafe = 0)); }
  }
}

machine UnsafeOverwriteTest {
  start state Init {
    entry { new TransportModel((scenario = 1, unsafe = 1)); }
  }
}

machine UnsafePartialReplayTest {
  start state Init {
    entry { new TransportModel((scenario = 3, unsafe = 2)); }
  }
}

machine UnsafeUnknownReplayTest {
  start state Init {
    entry { new TransportModel((scenario = 3, unsafe = 3)); }
  }
}

machine UnsafeLeaseReleaseTest {
  start state Init {
    entry { new TransportModel((scenario = 2, unsafe = 4)); }
  }
}

machine UnsafeStaleHandleTest {
  start state Init {
    entry { new TransportModel((scenario = 9, unsafe = 5)); }
  }
}
