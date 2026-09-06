spec NoOverwrite observes ePublish, eConsume, eFence {
  var live: map[(int, int), tFrame];
  start state Watching {
    on ePublish do (frame: tFrame) {
      var slot: (int, int);
      slot = (frame.lane, frame.sequence % 4);
      assert !(slot in live), "published over a live slot";
      live[slot] = frame;
    }
    on eConsume do (frame: tFrame) {
      var slot: (int, int);
      slot = (frame.lane, frame.sequence % 4);
      assert slot in live, "consumed an unpublished slot";
      assert live[slot] == frame, "consumed a different slot incarnation";
      live -= (slot);
    }
    on eFence do (epoch: tEpoch) {
      var i: int;
      i = 0;
      while (i < 4) {
        if ((epoch.lane, i) in live) live -= ((epoch.lane, i));
        i = i + 1;
      }
    }
  }
}

spec NoDuplicateConsume observes eConsume, eExecute, eResponse {
  var consumed: set[(int, int, int)];
  var executed: set[int];
  var responses: set[int];
  start state Watching {
    on eConsume do (frame: tFrame) {
      var key: (int, int, int);
      key = (frame.lane, frame.generation, frame.sequence);
      assert !(key in consumed), "duplicate cell consume";
      consumed += (key);
    }
    on eExecute do (id: int) {
      assert !(id in executed), "duplicate request execution";
      executed += (id);
    }
    on eResponse do (id: int) {
      assert id in executed, "response before execution";
      assert !(id in responses), "duplicate response completion";
      responses += (id);
    }
  }
}

spec NoStaleGeneration observes eOpen, ePublish, eConsume, eExport, eFence, eHandleAccess {
  var current: map[int, int];
  var accessible: set[tEpoch];
  start state Watching {
    on eOpen do (epoch: tEpoch) {
      if (epoch.lane in current) {
        assert epoch.generation == current[epoch.lane] + 1, "generation must advance exactly once";
        assert !((lane = epoch.lane, generation = current[epoch.lane]) in accessible),
               "generation reused before old owner fence";
      }
      current[epoch.lane] = epoch.generation;
    }
    on ePublish do (frame: tFrame) {
      assert current[frame.lane] == frame.generation, "stale generation published";
    }
    on eConsume do (frame: tFrame) {
      assert current[frame.lane] == frame.generation, "stale generation consumed";
    }
    on eExport do (epoch: tEpoch) { accessible += (epoch); }
    on eFence do (epoch: tEpoch) { accessible -= (epoch); }
    on eHandleAccess do (handle: tHandle) {
      if (handle.accepted) {
        assert current[handle.lane] == handle.generation, "stale allocation handle accepted";
        assert (lane = handle.lane, generation = handle.generation) in accessible,
               "retired allocation handle accepted";
        assert handle.offset >= 0 && handle.length > 0 && handle.offset + handle.length <= 16,
               "subrange escaped allocation";
      }
    }
  }
}

spec OrderedStreamWatermarks observes eOpen, eAccept, ePublish, eDeliver, eReserve, eExecute {
  var accepted: map[int, int];
  var published: map[int, int];
  var delivered: map[int, int];
  var requests: map[int, tRequest];
  start state Watching {
    on eOpen do (epoch: tEpoch) {
      accepted[epoch.lane] = 0; published[epoch.lane] = 0; delivered[epoch.lane] = 0;
    }
    on eReserve do (request: tRequest) { requests[request.id] = request; }
    on eAccept do (position: tWatermark) {
      assert position.offset >= accepted[position.lane], "accepted watermark moved backwards";
      accepted[position.lane] = position.offset;
    }
    on ePublish do (frame: tFrame) {
      assert frame.begin == published[frame.lane] && frame.end > frame.begin,
             "published stream has a gap or overlap";
      assert frame.end <= accepted[frame.lane], "published beyond accepted bytes";
      published[frame.lane] = frame.end;
    }
    on eDeliver do (position: tWatermark) {
      assert position.offset >= delivered[position.lane], "delivered watermark moved backwards";
      assert position.offset <= published[position.lane], "delivered beyond published bytes";
      delivered[position.lane] = position.offset;
    }
    on eExecute do (id: int) {
      assert requests[id].end <= delivered[requests[id].lane], "executed before complete delivery";
    }
  }
}

spec NoFalseRejectedBeforeExecute observes eReserve, eExecute, eDeliver, eOpen, eRetire, eResolve {
  var requests: map[int, tRequest];
  var executed: set[int];
  var delivered: map[int, int];
  var retired: map[tEpoch, bool];
  start state Watching {
    on eReserve do (request: tRequest) { requests[request.id] = request; }
    on eExecute do (id: int) { executed += (id); }
    on eOpen do (epoch: tEpoch) { delivered[epoch.lane] = 0; }
    on eDeliver do (position: tWatermark) { delivered[position.lane] = position.offset; }
    on eRetire do (retirement: tRetirement) {
      retired[(lane = retirement.lane, generation = retirement.generation)] = retirement.trustworthy;
    }
    on eResolve do (resolution: tResolution) {
      var request: tRequest;
      var epoch: tEpoch;
      request = requests[resolution.id];
      epoch = (lane = request.lane, generation = request.generation);
      assert epoch in retired, "request resolved from a timeout without retirement";
      if (resolution.disposition == 1) {
        assert retired[epoch], "rejected from an untrustworthy delivered record";
        assert !(resolution.id in executed), "executed request classified as rejected";
        assert delivered[request.lane] <= request.begin, "partially delivered request classified as rejected";
      }
    }
  }
}

spec NoUnknownReplay observes eResolve, eRetry {
  var resolutions: map[int, int];
  start state Watching {
    on eResolve do (resolution: tResolution) { resolutions[resolution.id] = resolution.disposition; }
    on eRetry do (id: int) {
      assert id in resolutions && resolutions[id] == 1, "unknown request automatically replayed";
    }
  }
}

spec NoPrematureRelease observes eExport, eFence, eRelease {
  var accessible: set[tEpoch];
  var retained: set[tEpoch];
  start state Watching {
    on eExport do (epoch: tEpoch) {
      assert !(epoch in retained), "allocation exported twice";
      accessible += (epoch); retained += (epoch);
    }
    on eFence do (epoch: tEpoch) { accessible -= (epoch); }
    on eRelease do (epoch: tEpoch) {
      assert !(epoch in accessible), "lease released while old handle remains accessible";
      assert epoch in retained, "lease released twice";
      retained -= (epoch);
    }
  }
}

spec EventuallyDrained observes eAccept, eDeliver, eFence, ePeerUnavailable {
  var pending: map[int, int];
  var delivered: map[int, int];
  fun UpdateAccept(position: tWatermark) {
    if (!(position.lane in delivered)) delivered[position.lane] = 0;
    pending[position.lane] = position.offset;
  }
  fun CheckDone() {
    var lane: int;
    foreach (lane in keys(pending)) {
      if (pending[lane] != delivered[lane]) return;
    }
    goto Idle;
  }
  fun Deliver(position: tWatermark) {
    delivered[position.lane] = position.offset;
    CheckDone();
  }
  fun Retire(lane: int) {
    pending[lane] = 0; delivered[lane] = 0;
    CheckDone();
  }
  start cold state Idle {
    on eAccept goto Pending with UpdateAccept;
    on eFence do (epoch: tEpoch) { Retire(epoch.lane); }
    on ePeerUnavailable do Retire;
    on eDeliver do Deliver;
  }
  hot state Pending {
    on eAccept do UpdateAccept;
    on eDeliver do Deliver;
    on eFence do (epoch: tEpoch) { Retire(epoch.lane); }
    on ePeerUnavailable do Retire;
  }
}
