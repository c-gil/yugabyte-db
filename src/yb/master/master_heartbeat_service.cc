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

#include "yb/common/common_flags.h"

#include "yb/master/catalog_entity_info.pb.h"
#include "yb/master/catalog_manager.h"
#include "yb/master/master_heartbeat.service.h"
#include "yb/master/master_service_base.h"
#include "yb/master/master_service_base-internal.h"
#include "yb/master/ts_manager.h"

#include "yb/util/flag_tags.h"

DEFINE_int32(tablet_report_limit, 1000,
             "Max Number of tablets to report during a single heartbeat. "
             "If this is set to INT32_MAX, then heartbeat will report all dirty tablets.");
TAG_FLAG(tablet_report_limit, advanced);

DECLARE_int32(heartbeat_rpc_timeout_ms);

DECLARE_CAPABILITY(TabletReportLimit);

using namespace std::literals;

namespace yb {
namespace master {

namespace {

class MasterHeartbeatServiceImpl : public MasterServiceBase, public MasterHeartbeatIf {
 public:
  explicit MasterHeartbeatServiceImpl(Master* master)
      : MasterServiceBase(master), MasterHeartbeatIf(master->metric_entity()) {}

  void TSHeartbeat(const TSHeartbeatRequestPB* req,
                   TSHeartbeatResponsePB* resp,
                   rpc::RpcContext rpc) override {
    LongOperationTracker long_operation_tracker("TSHeartbeat", 1s);

    // If CatalogManager is not initialized don't even know whether or not we will
    // be a leader (so we can't tell whether or not we can accept tablet reports).
    SCOPED_LEADER_SHARED_LOCK(l, server_->catalog_manager_impl());

    consensus::ConsensusStatePB cpb;
    Status s = server_->catalog_manager_impl()->GetCurrentConfig(&cpb);
    if (!s.ok()) {
      // For now, we skip setting the config on errors (hopefully next heartbeat will work).
      // We could enhance to fail rpc, if there are too many error, on a case by case error basis.
      LOG(WARNING) << "Could not set master raft config : " << s.ToString();
    } else if (cpb.has_config()) {
      if (cpb.config().opid_index() > req->config_index()) {
        *resp->mutable_master_config() = std::move(cpb.config());
        LOG(INFO) << "Set config at index " << resp->master_config().opid_index() << " for ts uuid "
                  << req->common().ts_instance().permanent_uuid();
      }
    } // Do nothing if config not ready.

    if (!l.CheckIsInitializedAndIsLeaderOrRespond(resp, &rpc)) {
      resp->set_leader_master(false);
      return;
    }

    resp->mutable_master_instance()->CopyFrom(server_->instance_pb());
    resp->set_leader_master(true);

    // If the TS is registering, register in the TS manager.
    if (req->has_registration()) {
      Status s = server_->ts_manager()->RegisterTS(req->common().ts_instance(),
                                                   req->registration(),
                                                   server_->MakeCloudInfoPB(),
                                                   &server_->proxy_cache());
      if (!s.ok()) {
        LOG(WARNING) << "Unable to register tablet server (" << rpc.requestor_string() << "): "
                     << s.ToString();
        // TODO: add service-specific errors.
        rpc.RespondFailure(s);
        return;
      }
      SysClusterConfigEntryPB cluster_config;
      s = server_->catalog_manager_impl()->GetClusterConfig(&cluster_config);
      if (!s.ok()) {
        LOG(WARNING) << "Unable to get cluster configuration: " << s.ToString();
        rpc.RespondFailure(s);
      }
      resp->set_cluster_uuid(cluster_config.cluster_uuid());
    }

    s = server_->catalog_manager_impl()->FillHeartbeatResponse(req, resp);
    if (!s.ok()) {
      LOG(WARNING) << "Unable to fill heartbeat response: " << s.ToString();
      rpc.RespondFailure(s);
    }

    // Look up the TS -- if it just registered above, it will be found here.
    // This allows the TS to register and tablet-report in the same RPC.
    TSDescriptorPtr ts_desc;
    s = server_->ts_manager()->LookupTS(req->common().ts_instance(), &ts_desc);
    if (s.IsNotFound()) {
      LOG(INFO) << "Got heartbeat from unknown tablet server { "
                << req->common().ts_instance().ShortDebugString()
                << " } as " << rpc.requestor_string()
                << "; Asking this server to re-register.";
      resp->set_needs_reregister(true);
      resp->set_needs_full_tablet_report(true);
      rpc.RespondSuccess();
      return;
    } else if (!s.ok()) {
      LOG(WARNING) << "Unable to look up tablet server for heartbeat request "
                   << req->DebugString() << " from " << rpc.requestor_string()
                   << "\nStatus: " << s.ToString();
      rpc.RespondFailure(s.CloneAndPrepend("Unable to lookup TS"));
      return;
    }

    ts_desc->UpdateHeartbeat(req);

    // Adjust the table report limit per heartbeat so this can be dynamically changed.
    if (ts_desc->HasCapability(CAPABILITY_TabletReportLimit)) {
      resp->set_tablet_report_limit(FLAGS_tablet_report_limit);
    }

    // Set the TServer metrics in TS Descriptor.
    if (req->has_metrics()) {
      ts_desc->UpdateMetrics(req->metrics());
    }

    if (req->has_tablet_report()) {
      s = server_->catalog_manager_impl()->ProcessTabletReport(
        ts_desc.get(), req->tablet_report(), resp->mutable_tablet_report(), &rpc);
      if (!s.ok()) {
        rpc.RespondFailure(s.CloneAndPrepend("Failed to process tablet report"));
        return;
      }
    }

    if (!req->has_tablet_report() || req->tablet_report().is_incremental()) {
      // Only process split tablets if we have plenty of time to process the work
      // (> 50% of timeout).
      auto safe_time_left = CoarseMonoClock::Now() + (FLAGS_heartbeat_rpc_timeout_ms * 1ms / 2);

      safe_time_left = CoarseMonoClock::Now() + (FLAGS_heartbeat_rpc_timeout_ms * 1ms / 2);
      if (rpc.GetClientDeadline() > safe_time_left) {
        for (const auto& storage_metadata : req->storage_metadata()) {
          server_->catalog_manager_impl()->ProcessTabletStorageMetadata(
                ts_desc.get()->permanent_uuid(), storage_metadata);
        }
      }

      // Only set once. It may take multiple heartbeats to receive a full tablet report.
      if (!ts_desc->has_tablet_report()) {
        resp->set_needs_full_tablet_report(true);
      }
    }

    // Retrieve all the nodes known by the master.
    std::vector<std::shared_ptr<TSDescriptor>> descs;
    server_->ts_manager()->GetAllLiveDescriptors(&descs);
    for (const auto& desc : descs) {
      *resp->add_tservers() = *desc->GetTSInformationPB();
    }

    // Retrieve the ysql catalog schema version.
    uint64_t last_breaking_version = 0;
    uint64_t catalog_version = 0;
    s = server_->catalog_manager_impl()->GetYsqlCatalogVersion(
        &catalog_version, &last_breaking_version);
    if (s.ok()) {
      resp->set_ysql_catalog_version(catalog_version);
      resp->set_ysql_last_breaking_catalog_version(last_breaking_version);
      if (FLAGS_log_ysql_catalog_versions) {
        VLOG_WITH_FUNC(1) << "responding (to ts " << req->common().ts_instance().permanent_uuid()
                          << ") catalog version: " << catalog_version
                          << ", breaking version: " << last_breaking_version;
      }
    } else {
      LOG(WARNING) << "Could not get YSQL catalog version for heartbeat response: "
                   << s.ToUserMessage();
    }

    uint64_t txn_table_versions_hash = server_->catalog_manager()->GetTxnTableVersionsHash();
    resp->set_txn_table_versions_hash(txn_table_versions_hash);

    rpc.RespondSuccess();
  }

};

} // namespace

std::unique_ptr<rpc::ServiceIf> MakeMasterHeartbeatService(Master* master) {
  return std::make_unique<MasterHeartbeatServiceImpl>(master);
}

} // namespace master
} // namespace yb
