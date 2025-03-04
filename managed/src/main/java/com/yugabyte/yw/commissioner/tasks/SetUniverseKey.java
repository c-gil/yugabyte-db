/*
 * Copyright 2019 YugaByte, Inc. and Contributors
 *
 * Licensed under the Polyform Free Trial License 1.0.0 (the "License"); you
 * may not use this file except in compliance with the License. You
 * may obtain a copy of the License at
 *
 *     https://github.com/YugaByte/yugabyte-db/blob/master/licenses/POLYFORM-FREE-TRIAL-LICENSE-1.0.0.txt
 */

package com.yugabyte.yw.commissioner.tasks;

import com.yugabyte.yw.commissioner.BaseTaskDependencies;
import com.yugabyte.yw.commissioner.SubTaskGroup;
import com.yugabyte.yw.commissioner.SubTaskGroupQueue;
import com.yugabyte.yw.commissioner.UserTaskDetails.SubTaskGroupType;
import com.yugabyte.yw.forms.EncryptionAtRestKeyParams;
import javax.inject.Inject;
import lombok.extern.slf4j.Slf4j;

@Slf4j
public class SetUniverseKey extends UniverseTaskBase {

  @Inject
  protected SetUniverseKey(BaseTaskDependencies baseTaskDependencies) {
    super(baseTaskDependencies);
  }

  @Override
  protected EncryptionAtRestKeyParams taskParams() {
    return (EncryptionAtRestKeyParams) taskParams;
  }

  @Override
  public void run() {
    log.info("Started {} task.", getName());
    try {
      checkUniverseVersion();
      // Create the task list sequence.
      subTaskGroupQueue = new SubTaskGroupQueue(userTaskUUID);

      // Update the universe DB with the update to be performed and set the
      // 'updateInProgress' flag to prevent other updates from happening.
      lockUniverseForUpdate(taskParams().expectedUniverseVersion);

      preTaskActions();

      // Manage encryption at rest
      SubTaskGroup manageEncryptionKeyTask = createManageEncryptionAtRestTask();
      if (manageEncryptionKeyTask != null) {
        manageEncryptionKeyTask.setSubTaskGroupType(SubTaskGroupType.ConfigureUniverse);
      }

      // Marks the update of this universe as a success only if all the tasks before it succeeded.
      createMarkUniverseUpdateSuccessTasks()
          .setSubTaskGroupType(SubTaskGroupType.ConfigureUniverse);

      // Run all the tasks.
      subTaskGroupQueue.run();
    } catch (Throwable t) {
      log.error("Error executing task {}, error='{}'", getName(), t.getMessage(), t);
      throw t;
    } finally {
      // Mark the update of the universe as done. This will allow future edits/updates to the
      // universe to happen.
      unlockUniverseForUpdate();
    }
    log.info("Finished {} task.", getName());
  }
}
