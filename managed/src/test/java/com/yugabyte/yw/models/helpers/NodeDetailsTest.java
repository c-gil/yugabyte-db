// Copyright (c) YugaByte, Inc.
package com.yugabyte.yw.models.helpers;

import static org.hamcrest.CoreMatchers.equalTo;
import static org.hamcrest.CoreMatchers.notNullValue;
import static org.hamcrest.core.AllOf.allOf;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertThat;
import static org.junit.Assert.assertTrue;

import com.yugabyte.yw.common.ApiUtils;
import java.util.HashSet;
import java.util.Set;
import org.junit.Before;
import org.junit.Test;

public class NodeDetailsTest {
  private NodeDetails nd;

  @Before
  public void setUp() {
    nd = ApiUtils.getDummyNodeDetails(1, NodeDetails.NodeState.Live);
  }

  @Test
  public void testToString() {
    assertThat(
        nd.toString(),
        allOf(
            notNullValue(),
            equalTo(
                "name: host-n1, cloudInfo: az-1.test-region.aws, type: "
                    + ApiUtils.UTIL_INST_TYPE
                    + ", ip: host-n1, "
                    + "isMaster: false, isTserver: true, state: Live, "
                    + "azUuid: null, placementUuid: null")));
  }

  @Test
  public void testIsActive() {
    Set<NodeDetails.NodeState> activeStates = new HashSet<>();
    activeStates.add(NodeDetails.NodeState.ToBeAdded);
    activeStates.add(NodeDetails.NodeState.InstanceCreated);
    activeStates.add(NodeDetails.NodeState.ServerSetup);
    activeStates.add(NodeDetails.NodeState.ToJoinCluster);
    activeStates.add(NodeDetails.NodeState.Provisioned);
    activeStates.add(NodeDetails.NodeState.SoftwareInstalled);
    activeStates.add(NodeDetails.NodeState.UpgradeSoftware);
    activeStates.add(NodeDetails.NodeState.UpdateGFlags);
    activeStates.add(NodeDetails.NodeState.UpdateCert);
    activeStates.add(NodeDetails.NodeState.ToggleTls);
    activeStates.add(NodeDetails.NodeState.Live);
    activeStates.add(NodeDetails.NodeState.Stopping);
    activeStates.add(NodeDetails.NodeState.Resizing);
    for (NodeDetails.NodeState state : NodeDetails.NodeState.values()) {
      nd.state = state;
      if (activeStates.contains(state)) {
        assertTrue("Node is inactive unexpectedly. State: " + state, nd.isActive());
      } else {
        assertFalse("Node is active unexpectedly. State: " + state, nd.isActive());
      }
    }
  }

  @Test
  public void testIsQueryable() {
    Set<NodeDetails.NodeState> queryableStates = new HashSet<>();
    queryableStates.add(NodeDetails.NodeState.UpgradeSoftware);
    queryableStates.add(NodeDetails.NodeState.UpdateGFlags);
    queryableStates.add(NodeDetails.NodeState.UpdateCert);
    queryableStates.add(NodeDetails.NodeState.ToggleTls);
    queryableStates.add(NodeDetails.NodeState.Live);
    queryableStates.add(NodeDetails.NodeState.ToBeRemoved);
    queryableStates.add(NodeDetails.NodeState.Removing);
    queryableStates.add(NodeDetails.NodeState.Stopping);
    for (NodeDetails.NodeState state : NodeDetails.NodeState.values()) {
      nd.state = state;
      if (queryableStates.contains(state)) {
        assertTrue(nd.isQueryable());
      } else {
        assertFalse(nd.isQueryable());
      }
    }
  }
}
