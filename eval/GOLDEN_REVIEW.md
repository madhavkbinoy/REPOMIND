# Golden set review

50 questions. Every metric in the project depends on these being sound.
**9 auto-flagged** for review (marked ⚠️ and pre-filled REJECT). The other 41 need your eyes.


**How to use:** read each question. If it is bad, add `REJECT` on the verdict line.
Then tell me and I will apply it to `eval/golden.jsonl` and re-run the evaluation.

**Reject a question if:**
- it is not about kubelet (kube-proxy, scheduler, apiserver — these crept in via PR back-references)
- it could not be asked by someone who has not already read the source document
- it asks about trivia (dates, version bumps, who said what) rather than design rationale
- it is answerable from general Kubernetes knowledge without this corpus

`overlap` is how much question vocabulary is shared with the source document.
High values (>0.8) inflate keyword search; a set full of them measures the wrong thing.

---

### 1. issue #72881

> Why does the kubelet’s --system-reserved flag apply a memory limit to the system.slice cgroup but leave the CPU quota unlimited, rather than enforcing both resources equally?

- source: [“--system-reserved” kubelet option cannot work as expected](https://github.com/kubernetes/kubernetes/issues/72881)
- 50 comments · lexical overlap 0.50
- **verdict:** 

### 2. issue #104432

> Why does kubelet allow pulling public images without failing when a pod’s imagePullSecrets references a non‑existent secret?

- source: [imagePullSecrets should log warning if secret does not exist](https://github.com/kubernetes/kubernetes/issues/104432)
- 50 comments · lexical overlap 0.69
- **verdict:** 

### 3. issue #18174  ⚠️ NOT-KUBELET

> Why does the kube‑proxy design require it to accept a list of API‑server endpoints instead of using the service IP for HA without an external load balancer?

- source: ["don't require a load balancer between cluster and control plane and sti](https://github.com/kubernetes/kubernetes/issues/18174)
- 50 comments · lexical overlap 0.62
- **verdict:** REJECT

### 4. issue #26093

> Why is glibc regarded as an unnecessary hard dependency for kubelet and targeted for removal?

- source: [Identify, document and maybe remove kubelet dependencies](https://github.com/kubernetes/kubernetes/issues/26093)
- 50 comments · lexical overlap 0.62
- **verdict:** 

### 5. issue #6059  ⚠️ NOT-KUBELET

> Why does the scheduler experience a ~45‑second delay before attempting to schedule newly created pods when watch streams encounter “unexpected EOF” and “event index outdated” errors?

- source: [Scheduler & Kubelet are not responding immediately on work to do](https://github.com/kubernetes/kubernetes/issues/6059)
- 50 comments · lexical overlap 0.53
- **verdict:** REJECT

### 6. issue #4710

> Why does the design propose restarting containers when a secret that is exposed as an environment variable changes?

- source: [Expose secrets to containers in environment variables](https://github.com/kubernetes/kubernetes/issues/4710)
- 50 comments · lexical overlap 0.67
- **verdict:** 

### 7. issue #70585  ⚠️ HIGH-OVERLAP

> Why does the kubelet unconditionally add a CPU CFS quota for pods when `cpuCFSQuota` is enabled, even for Guaranteed QoS pods that use a cpuset?

- source: [Disable cpu quota(use only cpuset) for pod Guaranteed](https://github.com/kubernetes/kubernetes/issues/70585)
- 50 comments · lexical overlap 0.93
- **verdict:** REJECT

### 8. issue #44976

> Why does the CRI logging design propose that runtimes escape newlines in log content and always append a newline, with kubelet then unescaping them?

- source: [CRI: Define CRI log format better](https://github.com/kubernetes/kubernetes/issues/44976)
- 50 comments · lexical overlap 0.60
- **verdict:** 

### 9. issue #60987

> Why does kubelet consider the presence of volume paths on disk for an orphaned pod an error condition and avoid automatically deleting those directories?

- source: [Orphaned pod found - but volume paths are still present on disk](https://github.com/kubernetes/kubernetes/issues/60987)
- 50 comments · lexical overlap 0.60
- **verdict:** 

### 10. issue #80968

> What was the reasoning behind the node‑lifecycle controller first adding a NoSchedule taint and, after a 5‑second delay, adding a NoExecute taint to a node that becomes unreachable?

- source: [Pods on node with temporary unknown status never marked ready again](https://github.com/kubernetes/kubernetes/issues/80968)
- 50 comments · lexical overlap 0.73
- **verdict:** 

### 11. issue #47448  ⚠️ NOT-KUBELET

> Why does the client‑side validation in the kubelet/kubectl need to tolerate unknown schema types to maintain the claimed +/- 1 version skew compatibility?

- source: [PodSecurityContext API compatibility broken between 1.6 and 1.7](https://github.com/kubernetes/kubernetes/issues/47448)
- 50 comments · lexical overlap 0.80
- **verdict:** REJECT

### 12. issue #88986  ⚠️ NOT-KUBELET

> Why does kube‑proxy bind a Service’s NodePort on every cluster node instead of only on the nodes that host the Service’s pod endpoints?

- source: [Bare Metal K8S 63 Second Service Routing Delay - when accessing service ](https://github.com/kubernetes/kubernetes/issues/88986)
- 50 comments · lexical overlap 0.50
- **verdict:** REJECT

### 13. issue #23104

> What was the design rationale for having the kubelet kill and recreate containers when the kubelet binary is upgraded without first draining the node?

- source: [Upgrading a node from kubelet 1.1.3 to 1.2.0 results in containers getti](https://github.com/kubernetes/kubernetes/issues/23104)
- 50 comments · lexical overlap 0.54
- **verdict:** 

### 14. issue #31272

> Why does the kubelet’s volume teardown abort the entire operation when IsLikelyNotMountPoint fails, causing subsequent volume actions to be blocked?

- source: [Hung volumes can wedge the kubelet](https://github.com/kubernetes/kubernetes/issues/31272)
- 50 comments · lexical overlap 0.58
- **verdict:** 

### 15. issue #100277

> Why does kubelet intentionally mark containers as not ready after a kubelet restart rather than assuming they are ready until a new probe confirms their state?

- source: [pod config readinessprobe, if kubelet restart,  pod temporarily report c](https://github.com/kubernetes/kubernetes/issues/100277)
- 50 comments · lexical overlap 0.67
- **verdict:** 

### 16. issue #29838  ⚠️ NOT-KUBELET

> Why does the Kubernetes events API (and consequently `kubectl get events`) not guarantee that events are returned sorted by their `lastSeen` timestamp?

- source: [kubectl get events doesnt sort events by last seen time.](https://github.com/kubernetes/kubernetes/issues/29838)
- 50 comments · lexical overlap 0.36
- **verdict:** REJECT

### 17. issue #27114

> Why does the kubelet start liveness probes before readiness probes by default rather than waiting for the readiness probe to succeed?

- source: [LivenessProbe should start after ReadinessProbe Succeeded if ReadinessPr](https://github.com/kubernetes/kubernetes/issues/27114)
- 50 comments · lexical overlap 0.82
- **verdict:** 

### 18. issue #43632

> Why does kubelet generate and mount a pod’s /etc/hosts file itself (using makeHostsMount and ensureHostsFile) instead of delegating host entry configuration to an external mechanism like a ConfigMap?

- source: [Provide a way to add entries to /etc/hosts](https://github.com/kubernetes/kubernetes/issues/43632)
- 50 comments · lexical overlap 0.70
- **verdict:** 

### 19. issue #1615

> Why did Kubernetes decide to share IPC namespaces across containers in a pod by default instead of isolating them per container?

- source: [Shared PID and UTS namespaces](https://github.com/kubernetes/kubernetes/issues/1615)
- 50 comments · lexical overlap 0.71
- **verdict:** 

### 20. issue #84210

> Why is adding support for an init process to containers considered necessary for kubelet’s design?

- source: [Support adding an init process to containers](https://github.com/kubernetes/kubernetes/issues/84210)
- 50 comments · lexical overlap 0.67
- **verdict:** 

### 21. pr #119186

> What was the reasoning for introducing a StreamTranslator proxy together with a FallbackExecutor and feature‑gate controls instead of directly replacing SPDY with WebSockets for the kubelet’s RemoteCommand streaming implementation?

- source: [Stream Translator Proxy and FallbackExecutor for WebSockets](https://github.com/kubernetes/kubernetes/issues/119186)
- 60 comments · lexical overlap 0.50
- **verdict:** 

### 22. pr #101432

> Why does the new CPU manager policy add constraints to reject non‑SMT‑aligned workloads in the static policy?

- source: [node: cpumanager: add options to reject non SMT-aligned workload](https://github.com/kubernetes/kubernetes/issues/101432)
- 60 comments · lexical overlap 0.67
- **verdict:** 

### 23. pr #9165

> Why was the TTL‑based graceful deletion removed and replaced by having the kubelet (and node controller) manage pod termination through reconciliation loops?

- source: [Enable graceful deletion using reconciliation loops in the Kubelet witho](https://github.com/kubernetes/kubernetes/issues/9165)
- 60 comments · lexical overlap 0.56
- **verdict:** 

### 24. pr #17353

> Why were external integer fields in the kubelet changed to uint32 or int32, and how does this relate to protobuf support?

- source: [Convert external int fields to uint32 or int32 as appropriate](https://github.com/kubernetes/kubernetes/issues/17353)
- 60 comments · lexical overlap 0.60
- **verdict:** 

### 25. pr #130701

> Why did the implementation add an alpha feature gate to guard the exposure of PSI metrics in the kubelet?

- source: [Surface Pressure Stall Information (PSI) metrics](https://github.com/kubernetes/kubernetes/issues/130701)
- 60 comments · lexical overlap 0.70
- **verdict:** 

### 26. pr #48859

> Why did the design opt to let users specify TLS cipher suites with a `--tls-cipher-suites` flag rather than using a fixed list of ciphers?

- source: [Support for custom tls cipher suites in api server and kubelet](https://github.com/kubernetes/kubernetes/issues/48859)
- 60 comments · lexical overlap 0.73
- **verdict:** 

### 27. pr #106907

> Why does kubelet eliminate the deprecated dockershim‑related command‑line flags when the dockershim component is removed?

- source: [Clean up dockershim flags in the kubelet](https://github.com/kubernetes/kubernetes/issues/106907)
- 60 comments · lexical overlap 0.60
- **verdict:** 

### 28. pr #96120  ⚠️ HIGH-OVERLAP

> Why was the NodeLogQuery feature gate introduced to provide administrators with a streaming view of node logs?

- source: [KEP 2258: add node log query](https://github.com/kubernetes/kubernetes/issues/96120)
- 60 comments · lexical overlap 0.90
- **verdict:** REJECT

### 29. pr #113374

> Why does the ClusterTrustBundle projected volume support sourcing trust anchors both by directly naming a ClusterTrustBundle and by using a signer name with a label selector?

- source: [Implement ClusterTrustBundlePEM projected volume](https://github.com/kubernetes/kubernetes/issues/113374)
- 60 comments · lexical overlap 0.80
- **verdict:** 

### 30. pr #127525

> Why does kubelet disable CFS quota enforcement for containers that satisfy the static cpu‑manager policy qualifications (Guaranteed QoS with integer CPU requests)?

- source: [fix: pods meeting qualifications for static placement when cpu-manager-p](https://github.com/kubernetes/kubernetes/issues/127525)
- 60 comments · lexical overlap 0.81
- **verdict:** 

### 31. pr #102913

> Why does the kubelet component eliminate dependencies such as github.com/alecthomas/units and cloud.google.com/go/pubsub during this update?

- source: [upgrade prometheus/common to v0.28.0](https://github.com/kubernetes/kubernetes/issues/102913)
- 60 comments · lexical overlap 0.58
- **verdict:** 

### 32. pr #90853

> Why was the Windows Docker shim updated to provide IPv6 dual‑stack support instead of handling this functionality directly within the kubelet?

- source: [KubeProxy and DockerShim changes for Ipv6 dual stack support on Windows](https://github.com/kubernetes/kubernetes/issues/90853)
- 60 comments · lexical overlap 0.33
- **verdict:** 

### 33. pr #13052

> Why does kubelet need a race‑condition fix to ensure the podIP is not blank when accessed via the downward API?

- source: [Fix race condition for consuming podIP via downward API](https://github.com/kubernetes/kubernetes/issues/13052)
- 60 comments · lexical overlap 0.75
- **verdict:** 

### 34. pr #103692

> Why is kubelet being rebuilt using Go 1.17 rather than the previous Go version?

- source: [[go1.17] Update to go1.17](https://github.com/kubernetes/kubernetes/issues/103692)
- 60 comments · lexical overlap 0.38
- **verdict:** 

### 35. pr #134639

> What was the reasoning for adding the `NodeSystemInfo.RunningInUserNamespace *bool` field when promoting the `KubeletInUserNamespace` feature to beta?

- source: [KEP-2033: promote KubeletInUserNamespace feature to beta (v1.37)](https://github.com/kubernetes/kubernetes/issues/134639)
- 60 comments · lexical overlap 0.67
- **verdict:** 

### 36. pr #29216

> Why did the author eliminate the legacy KubeletConfig type and make NewMainKubelet take only KubeletConfiguration (and KubeletDeps) to simplify the configuration path and improve accurate reporting of the Kubelet’s current config?

- source: [Refactor to simplify the hard-traveled path of the KubeletConfiguration ](https://github.com/kubernetes/kubernetes/issues/29216)
- 60 comments · lexical overlap 0.75
- **verdict:** 

### 37. pr #3763  ⚠️ HIGH-OVERLAP

> Why does the implementation currently use SPDY for streaming remote command execution and port forwarding, with a plan to move to HTTP/2 in the future?

- source: [Add streaming remote command execution and port forwarding](https://github.com/kubernetes/kubernetes/issues/3763)
- 60 comments · lexical overlap 0.93
- **verdict:** REJECT

### 38. pr #17922

> Why was the automatic encoding of `runtime.Object` during conversion disabled in this redesign?

- source: [Split codec from scheme](https://github.com/kubernetes/kubernetes/issues/17922)
- 60 comments · lexical overlap 0.71
- **verdict:** 

### 39. pr #18410

> Why is ReconcilePodStatus placed in the syncer component rather than the local status manager interface in the new RECONCILE design?

- source: [Add reconcile support in kubelet](https://github.com/kubernetes/kubernetes/issues/18410)
- 60 comments · lexical overlap 0.58
- **verdict:** 

### 40. pr #6949

> Why was node registration moved from the controller manager’s cloud‑provider loop to the kubelet so that nodes register themselves directly with the master?

- source: [Modify nodes to register directly with the master.](https://github.com/kubernetes/kubernetes/issues/6949)
- 60 comments · lexical overlap 0.79
- **verdict:** 

### 41. kep #4191

> Why does kubelet need to detect when the container runtime separates the image filesystem from the node filesystem?

- source: [Split Image Filesystem](https://github.com/kubernetes/enhancements/tree/master/keps/sig-node/4191-split-image-filesystem)
- 0 comments · lexical overlap 0.78
- **verdict:** 

### 42. kep #5945

> Why was the `SkipNodeOperations` field added to `ResourceSliceSpec`, and what design issue in the kubelet’s current DRA handling does it aim to resolve?

- source: [DRA Optional Node Preparation](https://github.com/kubernetes/enhancements/tree/master/keps/sig-node/5945-dra-optional-node-preparation)
- 0 comments · lexical overlap 0.58
- **verdict:** 

### 43. kep #5825

> Why does kubelet introduce server‑side streaming RPCs for listing containers and pod sandboxes instead of simply raising the gRPC message size limit?

- source: [CRI List Streaming](https://github.com/kubernetes/enhancements/tree/master/keps/sig-node/5825-cri-pagination)
- 0 comments · lexical overlap 0.53
- **verdict:** 

### 44. kep #277

> What is the reasoning behind the design decision that Ephemeral Containers are not restarted by the kubelet?

- source: [Ephemeral Containers](https://github.com/kubernetes/enhancements/tree/master/keps/sig-node/277-ephemeral-containers)
- 0 comments · lexical overlap 0.38
- **verdict:** 

### 45. kep #4622

> Why was the TopologyManager’s maxAllowableNUMANodes originally hard‑coded to 8?

- source: [New TopologyManager Policy which configure the value of maxAllowableNUMA](https://github.com/kubernetes/enhancements/tree/master/keps/sig-node/4622-topologymanager-max-allowable-numa-nodes)
- 0 comments · lexical overlap 0.40
- **verdict:** 

### 46. kep #2221

> Why does the kubelet design seek to remove the built‑in dockershim and treat Docker as just another external CRI implementation?

- source: [Removing dockershim from kubelet](https://github.com/kubernetes/enhancements/tree/master/keps/sig-node/2221-remove-dockershim)
- 0 comments · lexical overlap 0.69
- **verdict:** 

### 47. kep #1972

> Why does the design introduce a feature gate for exec probe timeouts instead of always enforcing the timeout?

- source: [Kubelet Exec Probe Timeouts](https://github.com/kubernetes/enhancements/tree/master/keps/sig-node/1972-kubelet-exec-probe-timeouts)
- 0 comments · lexical overlap 0.73
- **verdict:** 

### 48. kep #6063

> Why does the design require the kubelet to enforce the minimum of the node’s `podPidsLimit` and a pod’s `spec.resources.limits.pid` instead of allowing a pod‑specified limit to exceed the node‑level limit?

- source: [Per-Pod PID Limit](https://github.com/kubernetes/enhancements/tree/master/keps/sig-node/6063-pod-pid-limit)
- 0 comments · lexical overlap 0.80
- **verdict:** 

### 49. kep #2570  ⚠️ HIGH-OVERLAP

> Why does the Memory QoS design require the new `memoryReservationPolicy` field in KubeletConfiguration to start in Alpha?

- source: [Memory QoS](https://github.com/kubernetes/enhancements/tree/master/keps/sig-node/2570-memory-qos)
- 0 comments · lexical overlap 0.90
- **verdict:** REJECT

### 50. kep #6030

> Why is the ability to dynamically resize memory‑backed emptyDir volumes limited to nodes running with cgroup v2?

- source: [Dynamic Resize of Memory-Backed Volumes](https://github.com/kubernetes/enhancements/tree/master/keps/sig-node/6030-dynamic-resize-of-memory-backed-volumes)
- 0 comments · lexical overlap 0.82
- **verdict:** 
