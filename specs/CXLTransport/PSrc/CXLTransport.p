// Bounded abstraction of CxlLane, PublicationLedger and CxlBufferArena.
// An action is one owner publication or one receiver copy; nondeterministic
// action selection explores their interleavings without assuming immediate flush.
type tEpoch = (lane: int, generation: int);
type tWatermark = (lane: int, generation: int, offset: int);
type tFrame = (lane: int, generation: int, sequence: int, begin: int, end: int);
type tRequest = (id: int, lane: int, generation: int, begin: int, end: int);
type tRetirement = (lane: int, generation: int, trustworthy: bool);
type tResolution = (id: int, disposition: int);
type tHandle = (lane: int, generation: int, offset: int, length: int, accepted: bool);
event eOpen: tEpoch;
event eReserve: tRequest;
event eAccept: tWatermark;
event ePublish: tFrame;
event eDeliver: tWatermark;
event eConsume: tFrame;
event eExecute: int;
event eResponse: int;
event eTimeout: int;
event eRetire: tRetirement;
event eFence: tEpoch;
event eResolve: tResolution;
event eRetry: int;
event eExport: tEpoch;
event eRelease: tEpoch;
event eHandleAccess: tHandle;
event ePeerUnavailable: int;
event eBackpressure: int;
event eTick;

machine TransportModel {
  var scenario: int;
  var unsafe: int;
  var count: int;
  var steps: int;
  var generation: map[int, int];
  var reserved: map[int, int];
  var accepted: map[int, int];
  var published: map[int, int];
  var delivered: map[int, int];
  var producer: map[int, int];
  var consumer: map[int, int];
  var frames: map[(int, int), tFrame];
  var requests: map[int, tRequest];
  var executed: set[int];
  var responded: set[int];

  fun Open(lane: int) {
    var epoch: tEpoch;
    if (!(lane in generation)) generation[lane] = 0;
    generation[lane] = generation[lane] + 1;
    reserved[lane] = 0;
    accepted[lane] = 0;
    published[lane] = 0;
    delivered[lane] = 0;
    producer[lane] = 0;
    consumer[lane] = 0;
    epoch = (lane = lane, generation = generation[lane]);
    announce eOpen, epoch;
    announce eExport, epoch;
  }

  fun Reserve(lane: int, length: int) {
    var request: tRequest;
    request = (id = sizeof(requests), lane = lane, generation = generation[lane],
               begin = reserved[lane], end = reserved[lane] + length);
    requests[request.id] = request;
    reserved[lane] = request.end;
    announce eReserve, request;
  }

  fun Accept(lane: int, length: int) {
    if (length > reserved[lane] - accepted[lane]) length = reserved[lane] - accepted[lane];
    if (length == 0) return;
    accepted[lane] = accepted[lane] + length;
    announce eAccept, (lane = lane, generation = generation[lane], offset = accepted[lane]);
  }

  fun Publish(lane: int) {
    var length: int;
    var frame: tFrame;
    if (producer[lane] - consumer[lane] == 4) {
      announce eBackpressure, lane;
      return;
    }
    length = accepted[lane] - published[lane];
    if (length > 4) length = 4;
    if (length == 0) return;
    frame = (lane = lane, generation = generation[lane], sequence = producer[lane],
             begin = published[lane], end = published[lane] + length);
    frames[(lane, producer[lane] % 4)] = frame;
    producer[lane] = producer[lane] + 1;
    published[lane] = frame.end;
    announce ePublish, frame;
    if (unsafe == 1) announce ePublish, frame;
  }

  fun Deliver(lane: int, length: int) {
    var frame: tFrame;
    var i: int;
    var request: tRequest;
    if (consumer[lane] == producer[lane]) return;
    frame = frames[(lane, consumer[lane] % 4)];
    // Only a frame bound to the current generation is accepted.
    if (frame.generation != generation[lane]) return;
    if (length > frame.end - delivered[lane]) length = frame.end - delivered[lane];
    delivered[lane] = delivered[lane] + length;
    announce eDeliver, (lane = lane, generation = generation[lane], offset = delivered[lane]);
    if (delivered[lane] == frame.end) {
      consumer[lane] = consumer[lane] + 1;
      announce eConsume, frame;
    }
    i = 0;
    while (i < sizeof(requests)) {
      request = requests[i];
      if (request.lane == lane && request.generation == generation[lane] &&
          request.end <= delivered[lane] && !(i in executed)) {
        executed += (i);
        announce eExecute, i;
      }
      i = i + 1;
    }
  }

  fun Respond(id: int) {
    if (id in executed && !(id in responded)) {
      responded += (id);
      announce eResponse, id;
    }
  }

  fun Drain(lane: int) {
    var i: int;
    Accept(lane, reserved[lane]);
    while (delivered[lane] != accepted[lane]) {
      Publish(lane);
      Deliver(lane, 4);
    }
    i = 0;
    while (i < sizeof(requests)) {
      if (requests[i].lane == lane) Respond(i);
      i = i + 1;
    }
  }

  fun Retire(lane: int, trustworthy: bool) {
    var i: int;
    var request: tRequest;
    var disposition: int;
    announce eRetire, (lane = lane, generation = generation[lane], trustworthy = trustworthy);
    i = 0;
    while (i < sizeof(requests)) {
      request = requests[i];
      if (request.lane == lane && request.generation == generation[lane] && !(i in responded)) {
        // 0 Completed, 1 RejectedBeforeExecute, 2 OutcomeUnknown, 3 LaneRetired.
        disposition = 3;
        if (trustworthy) {
          disposition = 2;
          if (delivered[lane] <= request.begin) disposition = 1;
          // Intentionally unsafe: a partially delivered request is replayed.
          if (unsafe == 2 && delivered[lane] < request.end) disposition = 1;
        }
        announce eResolve, (id = i, disposition = disposition);
        if (disposition == 1 || unsafe == 3) announce eRetry, i;
      }
      i = i + 1;
    }
  }

  fun FenceAndRelease(lane: int) {
    var epoch: tEpoch;
    epoch = (lane = lane, generation = generation[lane]);
    // Models the verified owner-record retirement fence, never a timer.
    if (unsafe == 4) announce eRelease, epoch;
    announce eFence, epoch;
    announce eRelease, epoch;
  }

  fun AccessHandle(lane: int, handleGeneration: int, offset: int, length: int) {
    var valid: bool;
    // One 16-byte allocation slot; its export generation changes on reuse.
    valid = handleGeneration == generation[lane] && offset >= 0 && length > 0 && offset + length <= 16;
    if (unsafe == 5) valid = true;
    announce eHandleAccess, (lane = lane, generation = handleGeneration, offset = offset,
                            length = length, accepted = valid);
  }

  start state Init {
    entry (config: (scenario: int, unsafe: int)) {
      var i: int;
      scenario = config.scenario;
      unsafe = config.unsafe;
      count = 1;
      if (scenario == 0 || scenario == 6) count = 2;
      i = 0;
      while (i < count) { Open(i); i = i + 1; }

      if (scenario == 0) {
        i = 0;
        while (i < 8) {
          Reserve(0, choose(5) + 1); Drain(0);
          Reserve(1, choose(5) + 1); Drain(1);
          i = i + 1;
        }
        goto Done;
      }
      if (scenario == 1) {
        Reserve(0, 24); Accept(0, 24);
        i = 0;
        while (i < 5) { Publish(0); i = i + 1; }
        Drain(0);
        goto Done;
      }
      if (scenario == 2 || scenario == 3 || scenario == 7 || scenario == 8 || unsafe == 4) {
        Reserve(0, 3); Reserve(0, 3); Accept(0, 6);
        // Accepted-before-flush and partial delivery cover distinct timeout states.
        if (scenario != 2 || $) Publish(0);
        if (scenario == 3 || scenario == 7 || scenario == 8) Deliver(0, 4);
        if (scenario == 8) {
          Publish(0); Deliver(0, 4);
          announce ePeerUnavailable, 0;
          announce eTimeout, 0;
          Retire(0, false);
          // A crashed peer is quarantined: no fence, no release, no reuse.
          goto Quarantined;
        }
        announce eTimeout, 0;
        Retire(0, scenario != 7);
        FenceAndRelease(0);
        goto Quarantined;
      }
      if (scenario == 4 || scenario == 9) {
        Reserve(0, 3); Reserve(0, 3); Accept(0, 6); Publish(0); Deliver(0, 3);
        Retire(0, true); FenceAndRelease(0); Open(0);
        AccessHandle(0, 1, 0, 8);
        AccessHandle(0, 2, 15, 2);
        AccessHandle(0, 2, 4, 8);
        Reserve(0, 6); Drain(0);
        goto Done;
      }
      i = 0;
      while (i < count) {
        Reserve(i, 3); Reserve(i, 3); Reserve(i, 5); Reserve(i, 7);
        i = i + 1;
      }
      send this, eTick;
      goto Running;
    }
  }

  state Running {
    on eTick do {
      var lane: int;
      var action: int;
      var i: int;
      lane = choose(count);
      action = choose(5);
      if (action == 0) Accept(lane, choose(3) + 1);
      if (action == 1) Publish(lane);
      if (action == 2) Deliver(lane, choose(4) + 1);
      if (action == 3) Respond(choose(sizeof(requests)));
      if (action == 4) announce eTimeout, choose(sizeof(requests));
      steps = steps + 1;
      if (steps == 64) {
        // Bounded weak fairness: a live owner/receiver eventually runs.
        i = 0;
        while (i < count) { Drain(i); i = i + 1; }
        goto Done;
      }
      send this, eTick;
    }
  }

  state Done {
    entry {
      var i: int;
      i = 0;
      while (i < count) { Retire(i, true); FenceAndRelease(i); i = i + 1; }
    }
  }
  state Quarantined { }
}
