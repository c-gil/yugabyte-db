//
// Copyright (c) YugaByte, Inc.
//
// Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except
// in compliance with the License.  You may obtain a copy of the License at
//
// http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software distributed under the License
// is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
// or implied.  See the License for the specific language governing permissions and limitations
// under the License.
//
//

#ifndef YB_CLIENT_TABLET_RPC_H
#define YB_CLIENT_TABLET_RPC_H

#include <memory>
#include <string>
#include <unordered_set>

#include <gflags/gflags_declare.h>
#include <gtest/gtest_prod.h>

#include "yb/client/client_fwd.h"

#include "yb/common/hybrid_time.h"

#include "yb/master/master_fwd.h"

#include "yb/rpc/rpc_fwd.h"
#include "yb/rpc/rpc.h"

#include "yb/tserver/tserver_fwd.h"
#include "yb/tserver/tserver_types.pb.h"

#include "yb/util/status_fwd.h"
#include "yb/util/net/net_fwd.h"

namespace yb {

namespace tserver {
class TabletServerServiceProxy;
class TabletServerForwardServiceProxy;
}

namespace rpc {
class RpcController;
}

namespace client {
namespace internal {

class TabletRpc {
 public:
  virtual const tserver::TabletServerErrorPB* response_error() const = 0;
  virtual void Failed(const Status& status) = 0;

  // attempt_num starts with 1.
  virtual void SendRpcToTserver(int attempt_num) = 0;

  virtual bool ShouldRetryExpiredRequest() { return false; }

 protected:
  ~TabletRpc() {}
};

tserver::TabletServerErrorPB_Code ErrorCode(const tserver::TabletServerErrorPB* error);

class TabletInvoker {
 public:
  // If table is specified, TabletInvoker can detect that table partitions are stale in case tablet
  // is no longer available and return ClientErrorCode::kTablePartitionListIsStale.
  explicit TabletInvoker(const bool local_tserver_only,
                         const bool consistent_prefix,
                         YBClient* client,
                         rpc::RpcCommand* command,
                         TabletRpc* rpc,
                         RemoteTablet* tablet,
                         const std::shared_ptr<const YBTable>& table,
                         rpc::RpcRetrier* retrier,
                         Trace* trace,
                         master::IncludeInactive include_inactive =
                            master::IncludeInactive::kFalse);

  virtual ~TabletInvoker();

  void Execute(const std::string& tablet_id, bool leader_only = false);

  // Returns true when whole operation is finished, false otherwise.
  bool Done(Status* status);

  bool IsLocalCall() const;

  void WriteAsync(const tserver::WriteRequestPB& req, tserver::WriteResponsePB *resp,
                  rpc::RpcController *controller, std::function<void()>&& cb);

  void ReadAsync(const tserver::ReadRequestPB& req, tserver::ReadResponsePB *resp,
                 rpc::RpcController *controller, std::function<void()>&& cb);

  const RemoteTabletPtr& tablet() const { return tablet_; }
  std::shared_ptr<tserver::TabletServerServiceProxy> proxy() const;
  ::yb::HostPort ProxyEndpoint() const;
  YBClient& client() const { return *client_; }
  const RemoteTabletServer& current_ts() { return *current_ts_; }
  bool local_tserver_only() const { return local_tserver_only_; }

  bool is_consistent_prefix() const { return consistent_prefix_; }

 private:
  friend class TabletRpcTest;
  FRIEND_TEST(TabletRpcTest, TabletInvokerSelectTabletServerRace);

  void SelectTabletServer();

  // This is an implementation of ReadRpc with consistency level as CONSISTENT_PREFIX. As a result,
  // there is no requirement that the read needs to hit the leader.
  void SelectTabletServerWithConsistentPrefix();

  // This is for Redis ops which always prefer to invoke the local tablet server. In case when it
  // is not the leader, a MOVED response will be returned.
  void SelectLocalTabletServer();

  // Marks all replicas on current_ts_ as failed and retries the write on a
  // new replica.
  CHECKED_STATUS FailToNewReplica(const Status& reason,
                                  const tserver::TabletServerErrorPB* error_code = nullptr);

  // Called when we finish a lookup (to find the new consensus leader). Retries
  // the rpc after a short delay.
  void LookupTabletCb(const Result<RemoteTabletPtr>& result);

  void InitialLookupTabletDone(const Result<RemoteTabletPtr>& result);

  // If we receive TABLET_NOT_FOUND and current_ts_ is set, that means we contacted a tserver
  // with a tablet_id, but the tserver no longer has that tablet.
  bool TabletNotFoundOnTServer(const tserver::TabletServerErrorPB* error_code,
                               const Status& status) {
    return status.IsNotFound() &&
        ErrorCode(error_code) == tserver::TabletServerErrorPB::TABLET_NOT_FOUND &&
        current_ts_ != nullptr;
  }

  bool ShouldUseNodeLocalForwardProxy();

  YBClient* const client_;

  rpc::RpcCommand* const command_;

  TabletRpc* const rpc_;

  // The tablet that should receive this rpc.
  RemoteTabletPtr tablet_;

  std::string tablet_id_;

  const std::shared_ptr<const YBTable> table_;

  rpc::RpcRetrier* const retrier_;

  // Trace is provided externally and owner of this object should guarantee that it will be alive
  // while this object is alive.
  Trace* const trace_;

  // Whether or not to allow lookups of inactive (hidden) tablets.
  master::IncludeInactive const include_inactive_;

  // Used to retry some failed RPCs.
  // Tablet servers that refused the write because they were followers at the time.
  // Cleared when new consensus configuration information arrives from the master.
  struct FollowerData {
    // Last replica error, i.e. reason why it was marked as follower.
    Status status;
    // Error time.
    CoarseTimePoint time;

    std::string ToString() const;
  };

  std::unordered_map<RemoteTabletServer*, FollowerData> followers_;

  const bool local_tserver_only_;

  const bool consistent_prefix_;

  // The TS receiving the write. May change if the write is retried.
  // RemoteTabletServer is taken from YBClient cache, so it is guaranteed that those objects are
  // alive while YBClient is alive. Because we don't delete them, but only add and update.
  RemoteTabletServer* current_ts_ = nullptr;

  // Should we assign new leader in meta cache when successful response is received.
  bool assign_new_leader_ = false;

  // Whether to use the local node proxy or to use the default remote proxy for communication to the
  // tablet servers. This flag is true if all of the following conditions are true:
  // 1. FLAGS_ysql_forward_rpcs_to_local_tserver is true
  // 2. The node local forward proxy is set in the client.
  // 3. The destination tserver is not the same as the node local tserver.
  // 4. The rpc is not intended for the master.
  bool should_use_local_node_proxy_ = false;
};

CHECKED_STATUS ErrorStatus(const tserver::TabletServerErrorPB* error);
template <class Response>
HybridTime GetPropagatedHybridTime(const Response& response) {
  return response.has_propagated_hybrid_time() ? HybridTime(response.propagated_hybrid_time())
                                               : HybridTime::kInvalid;
}

} // namespace internal
} // namespace client
} // namespace yb

#endif // YB_CLIENT_TABLET_RPC_H
