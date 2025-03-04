/*
 * Copyright 2020 YugaByte, Inc. and Contributors
 *
 * Licensed under the Polyform Free Trial License 1.0.0 (the "License"); you
 * may not use this file except in compliance with the License. You
 * may obtain a copy of the License at
 *
 * http://github.com/YugaByte/yugabyte-db/blob/master/licenses/POLYFORM-FREE-TRIAL-LICENSE-1.0.0.txt
 */

package com.yugabyte.yw.common.alerts;

import static com.yugabyte.yw.common.metrics.MetricService.buildMetricTemplate;

import akka.actor.ActorSystem;
import com.google.common.annotations.VisibleForTesting;
import com.google.common.collect.Lists;
import com.google.inject.Inject;
import com.google.inject.Singleton;
import com.yugabyte.yw.common.AlertManager;
import com.yugabyte.yw.common.metrics.MetricService;
import com.yugabyte.yw.metrics.MetricQueryHelper;
import com.yugabyte.yw.metrics.data.AlertData;
import com.yugabyte.yw.metrics.data.AlertState;
import com.yugabyte.yw.models.Alert;
import com.yugabyte.yw.models.Alert.State;
import com.yugabyte.yw.models.AlertConfiguration;
import com.yugabyte.yw.models.AlertDefinition;
import com.yugabyte.yw.models.AlertLabel;
import com.yugabyte.yw.models.HighAvailabilityConfig;
import com.yugabyte.yw.models.filters.AlertConfigurationFilter;
import com.yugabyte.yw.models.filters.AlertDefinitionFilter;
import com.yugabyte.yw.models.filters.AlertFilter;
import com.yugabyte.yw.models.helpers.KnownAlertLabels;
import com.yugabyte.yw.models.helpers.PlatformMetrics;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.Date;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.Function;
import java.util.stream.Collectors;
import lombok.Value;
import lombok.extern.slf4j.Slf4j;
import org.apache.commons.collections.MapUtils;
import org.apache.commons.lang3.StringUtils;
import scala.concurrent.ExecutionContext;
import scala.concurrent.duration.Duration;

@Singleton
@Slf4j
public class QueryAlerts {

  private static final int YB_QUERY_ALERTS_INTERVAL_SEC = 30;
  private static final int ALERTS_BATCH = 1000;
  private static final String SUMMARY_ANNOTATION_NAME = "summary";

  private AtomicBoolean running = new AtomicBoolean(false);

  private final ActorSystem actorSystem;

  private final ExecutionContext executionContext;

  private final MetricQueryHelper queryHelper;

  private final MetricService metricService;

  private final AlertService alertService;

  private final AlertDefinitionService alertDefinitionService;

  private final AlertConfigurationService alertConfigurationService;

  private final AlertManager alertManager;

  @Inject
  public QueryAlerts(
      ExecutionContext executionContext,
      ActorSystem actorSystem,
      AlertService alertService,
      MetricQueryHelper queryHelper,
      MetricService metricService,
      AlertDefinitionService alertDefinitionService,
      AlertConfigurationService alertConfigurationService,
      AlertManager alertManager) {
    this.actorSystem = actorSystem;
    this.executionContext = executionContext;
    this.queryHelper = queryHelper;
    this.alertService = alertService;
    this.metricService = metricService;
    this.alertDefinitionService = alertDefinitionService;
    this.alertConfigurationService = alertConfigurationService;
    this.alertManager = alertManager;
  }

  public void start() {
    this.actorSystem
        .scheduler()
        .schedule(
            // Start 30 seconds later to allow Prometheus to get Platform metrics
            // and evaluate alerts based on them.
            Duration.create(YB_QUERY_ALERTS_INTERVAL_SEC, TimeUnit.SECONDS),
            Duration.create(YB_QUERY_ALERTS_INTERVAL_SEC, TimeUnit.SECONDS),
            this::scheduleRunner,
            this.executionContext);
  }

  @VisibleForTesting
  void scheduleRunner() {
    if (!running.compareAndSet(false, true)) {
      log.info("Previous run of alert query is still underway");
      return;
    }
    try {
      if (HighAvailabilityConfig.isFollower()) {
        log.debug("Skipping querying for alerts for follower platform");
        return;
      }
      try {
        List<UUID> activeAlertsUuids = processActiveAlerts();
        resolveAlerts(activeAlertsUuids);
        metricService.setOkStatusMetric(buildMetricTemplate(PlatformMetrics.ALERT_QUERY_STATUS));
      } catch (Exception e) {
        metricService.setStatusMetric(
            buildMetricTemplate(PlatformMetrics.ALERT_QUERY_STATUS),
            "Error querying for alerts: " + e.getMessage());
        log.error("Error querying for alerts", e);
      }
      alertManager.sendNotifications();
    } catch (Exception e) {
      log.error("Error processing alerts", e);
    } finally {
      running.set(false);
    }
  }

  private List<UUID> processActiveAlerts() {
    if (!queryHelper.isPrometheusManagementEnabled()) {
      return Collections.emptyList();
    }
    List<AlertData> alerts = queryHelper.queryAlerts();
    metricService.setMetric(
        buildMetricTemplate(PlatformMetrics.ALERT_QUERY_TOTAL_ALERTS), alerts.size());
    List<AlertData> validAlerts =
        alerts
            .stream()
            .filter(alertData -> getCustomerUuid(alertData) != null)
            .filter(alertData -> getConfigurationUuid(alertData) != null)
            .filter(alertData -> getDefinitionUuid(alertData) != null)
            .filter(alertData -> getSourceUuid(alertData) != null)
            .collect(Collectors.toList());
    if (alerts.size() > validAlerts.size()) {
      log.warn(
          "Found {} alerts without customer, configuration or definition uuid",
          alerts.size() - validAlerts.size());
    }
    metricService.setMetric(
        buildMetricTemplate(PlatformMetrics.ALERT_QUERY_INVALID_ALERTS),
        alerts.size() - validAlerts.size());

    List<AlertData> activeAlerts =
        validAlerts
            .stream()
            .filter(alertData -> alertData.getState() != AlertState.pending)
            .collect(Collectors.toList());
    metricService.setMetric(
        buildMetricTemplate(PlatformMetrics.ALERT_QUERY_PENDING_ALERTS),
        validAlerts.size() - activeAlerts.size());

    List<AlertData> deduplicatedAlerts =
        new ArrayList<>(
            activeAlerts
                .stream()
                .collect(
                    Collectors.toMap(
                        this::getAlertKey,
                        Function.identity(),
                        (a, b) ->
                            getSeverity(a).getPriority() > getSeverity(b).getPriority() ? a : b,
                        LinkedHashMap::new))
                .values());

    List<UUID> activeAlertUuids = new ArrayList<>();
    for (List<AlertData> batch : Lists.partition(deduplicatedAlerts, ALERTS_BATCH)) {
      Set<UUID> definitionUuids =
          batch
              .stream()
              .map(this::getDefinitionUuid)
              .map(UUID::fromString)
              .collect(Collectors.toSet());

      AlertFilter alertFilter =
          AlertFilter.builder()
              .definitionUuids(definitionUuids)
              .states(State.getFiringStates())
              .build();
      Map<AlertKey, Alert> existingAlertsByKey =
          alertService
              .list(alertFilter)
              .stream()
              .collect(Collectors.toMap(this::getAlertKey, Function.identity()));

      AlertDefinitionFilter definitionFilter =
          AlertDefinitionFilter.builder().uuids(definitionUuids).build();
      Map<UUID, AlertDefinition> existingDefinitionsByUuid =
          alertDefinitionService
              .list(definitionFilter)
              .stream()
              .collect(Collectors.toMap(AlertDefinition::getUuid, Function.identity()));

      Set<UUID> configurationUuids =
          existingDefinitionsByUuid
              .values()
              .stream()
              .map(AlertDefinition::getConfigurationUUID)
              .collect(Collectors.toSet());
      AlertConfigurationFilter configurationFilter =
          AlertConfigurationFilter.builder().uuids(configurationUuids).build();
      Map<UUID, AlertConfiguration> existingConfigsByUuid =
          alertConfigurationService
              .list(configurationFilter)
              .stream()
              .collect(Collectors.toMap(AlertConfiguration::getUuid, Function.identity()));

      List<Alert> toSave =
          batch
              .stream()
              .map(
                  data ->
                      processAlert(
                          data,
                          existingAlertsByKey,
                          existingDefinitionsByUuid,
                          existingConfigsByUuid))
              .filter(Objects::nonNull)
              .collect(Collectors.toList());
      metricService.setMetric(
          buildMetricTemplate(PlatformMetrics.ALERT_QUERY_FILTERED_ALERTS),
          activeAlerts.size() - toSave.size());
      long newAlerts = toSave.stream().filter(Alert::isNew).count();
      long updatedAlerts = toSave.size() - newAlerts;

      List<Alert> savedAlerts = alertService.save(toSave);
      metricService.setMetric(
          buildMetricTemplate(PlatformMetrics.ALERT_QUERY_NEW_ALERTS), newAlerts);
      metricService.setMetric(
          buildMetricTemplate(PlatformMetrics.ALERT_QUERY_UPDATED_ALERTS), updatedAlerts);

      activeAlertUuids.addAll(
          savedAlerts.stream().map(Alert::getUuid).collect(Collectors.toList()));
    }
    return activeAlertUuids;
  }

  private void resolveAlerts(List<UUID> activeAlertsUuids) {
    AlertFilter toResolveFilter = AlertFilter.builder().excludeUuids(activeAlertsUuids).build();
    List<Alert> resolved = alertService.markResolved(toResolveFilter);
    if (!resolved.isEmpty()) {
      log.info("Resolved {} alerts", resolved.size());
    }
    metricService.setMetric(
        buildMetricTemplate(PlatformMetrics.ALERT_QUERY_RESOLVED_ALERTS), resolved.size());
  }

  private String getCustomerUuid(AlertData alertData) {
    if (MapUtils.isEmpty(alertData.getLabels())) {
      return null;
    }
    return alertData.getLabels().get(KnownAlertLabels.CUSTOMER_UUID.labelName());
  }

  private String getDefinitionUuid(AlertData alertData) {
    if (MapUtils.isEmpty(alertData.getLabels())) {
      return null;
    }
    return alertData.getLabels().get(KnownAlertLabels.DEFINITION_UUID.labelName());
  }

  private String getConfigurationUuid(AlertData alertData) {
    if (MapUtils.isEmpty(alertData.getLabels())) {
      return null;
    }
    return alertData.getLabels().get(KnownAlertLabels.CONFIGURATION_UUID.labelName());
  }

  private String getSourceUuid(AlertData alertData) {
    if (MapUtils.isEmpty(alertData.getLabels())) {
      return null;
    }
    return alertData.getLabels().get(KnownAlertLabels.SOURCE_UUID.labelName());
  }

  private AlertKey getAlertKey(AlertData alertData) {
    return new AlertKey(getDefinitionUuid(alertData), getSourceUuid(alertData));
  }

  private AlertKey getAlertKey(Alert alert) {
    return new AlertKey(
        alert.getDefinitionUuid().toString(), alert.getLabelValue(KnownAlertLabels.SOURCE_UUID));
  }

  private AlertConfiguration.Severity getSeverity(AlertData alertData) {
    if (MapUtils.isEmpty(alertData.getLabels())) {
      return AlertConfiguration.Severity.SEVERE;
    }
    return Optional.ofNullable(alertData.getLabels().get(KnownAlertLabels.SEVERITY.labelName()))
        .map(AlertConfiguration.Severity::valueOf)
        .orElse(AlertConfiguration.Severity.SEVERE);
  }

  private AlertConfiguration.TargetType getConfigurationType(AlertData alertData) {
    if (MapUtils.isEmpty(alertData.getLabels())) {
      return AlertConfiguration.TargetType.UNIVERSE;
    }
    return Optional.ofNullable(
            alertData.getLabels().get(KnownAlertLabels.CONFIGURATION_TYPE.labelName()))
        .map(AlertConfiguration.TargetType::valueOf)
        .orElse(AlertConfiguration.TargetType.UNIVERSE);
  }

  private Alert processAlert(
      AlertData alertData,
      Map<AlertKey, Alert> existingAlertsByKey,
      Map<UUID, AlertDefinition> definitionsByUuid,
      Map<UUID, AlertConfiguration> configsByUuid) {
    AlertKey alertKey = getAlertKey(alertData);
    if (alertKey.getDefinitionUuid() == null) {
      // Should be filtered earlier
      log.error("Alert {} has no definition uuid", alertData);
      return null;
    }
    String configurationUuidStr = getConfigurationUuid(alertData);
    if (configurationUuidStr == null) {
      // Should be filtered earlier
      log.error("Alert {} has no configuration uuid", alertData);
      return null;
    }
    if (alertData.getState() == AlertState.pending) {
      // Should be filtered earlier
      log.error("Alert {} is in pending state - skip for now", alertData);
      return null;
    }
    UUID definitionUuid = UUID.fromString(alertKey.getDefinitionUuid());
    AlertDefinition definition = definitionsByUuid.get(definitionUuid);
    if (definition == null) {
      log.debug("Definition is missing for alert {}", alertData);
      return null;
    }
    UUID configurationUuid = UUID.fromString(configurationUuidStr);
    AlertConfiguration configuration = configsByUuid.get(configurationUuid);
    if (configuration == null || !configuration.isActive()) {
      log.debug("Alert configuration is missing or inactive for alert {}", alertData);
      return null;
    }
    Alert alert = existingAlertsByKey.get(alertKey);
    if (alert == null) {
      String customerUuid = alertData.getLabels().get(KnownAlertLabels.CUSTOMER_UUID.labelName());
      if (StringUtils.isEmpty(customerUuid)) {
        log.debug("Alert {} has no customer UUID", alertData);
        return null;
      }

      alert =
          new Alert()
              .setCreateTime(Date.from(alertData.getActiveAt().toInstant()))
              .setCustomerUUID(UUID.fromString(customerUuid))
              .setDefinitionUuid(definitionUuid)
              .setConfigurationUuid(configurationUuid)
              .setName(alertData.getLabels().get(KnownAlertLabels.DEFINITION_NAME.labelName()))
              .setSourceName(alertData.getLabels().get(KnownAlertLabels.SOURCE_NAME.labelName()))
              .setSourceUUID(UUID.fromString(alertKey.getSourceUuid()));
    }
    AlertConfiguration.Severity severity = getSeverity(alertData);
    AlertConfiguration.TargetType configurationType = getConfigurationType(alertData);
    String message = alertData.getAnnotations().get(SUMMARY_ANNOTATION_NAME);

    List<AlertLabel> labels =
        alertData
            .getLabels()
            .entrySet()
            .stream()
            .map(e -> new AlertLabel(e.getKey(), e.getValue()))
            .sorted(Comparator.comparing(AlertLabel::getName))
            .collect(Collectors.toList());
    alert
        .setSeverity(severity)
        .setConfigurationType(configurationType)
        .setMessage(message)
        .setLabels(labels);
    State state =
        alert.getLabelValue(KnownAlertLabels.MAINTENANCE_WINDOW_UUIDS) != null
            ? State.SUSPENDED
            : State.ACTIVE;
    if (alert.getState() != State.ACKNOWLEDGED) {
      alert.setState(state);
    }
    return alert;
  }

  @Value
  private static class AlertKey {
    String definitionUuid;
    String sourceUuid;
  }
}
