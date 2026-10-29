import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:http/http.dart' as http;
import 'package:workmanager/workmanager.dart';

const _notificationTask = 'pulsebrief-briefing-notification-poll';
const _notificationTitle = 'PulseBrief 每日 AI 简报';
const _enabledKey = 'background_notifications_enabled';
const _lastBriefingKey = 'background_notifications_last_briefing_id';
@visibleForTesting
const noPriorBriefingBaseline = '__no_prior_briefing__';

enum BackgroundNotificationEnableResult {
  enabled,
  unsupportedPlatform,
  permissionDenied,
  baselineUnavailable,
}

@visibleForTesting
BackgroundNotificationEnableResult notificationEnablePreflight({
  required bool isAndroid,
  required bool hasPermission,
  required bool baselineLoaded,
}) {
  if (!isAndroid) {
    return BackgroundNotificationEnableResult.unsupportedPlatform;
  }
  if (!hasPermission) {
    return BackgroundNotificationEnableResult.permissionDenied;
  }
  if (!baselineLoaded) {
    return BackgroundNotificationEnableResult.baselineUnavailable;
  }
  return BackgroundNotificationEnableResult.enabled;
}

const _storage = FlutterSecureStorage();
final _localNotifications = FlutterLocalNotificationsPlugin();

class _BriefingSnapshot {
  const _BriefingSnapshot({required this.succeeded, this.id});

  final bool succeeded;
  final String? id;
}

@visibleForTesting
String? newestBriefingId(Object? response) {
  if (response is! Map) return null;
  final items = response['items'];
  if (items is! List || items.isEmpty || items.first is! Map) return null;
  final id = items.first['id']?.toString().trim();
  return id == null || id.isEmpty ? null : id;
}

@visibleForTesting
bool isNewBriefing(String? currentId, String? lastNotifiedId) =>
    currentId != null &&
    currentId.isNotEmpty &&
    (lastNotifiedId == noPriorBriefingBaseline ||
        (lastNotifiedId != null &&
            lastNotifiedId.isNotEmpty &&
            currentId != lastNotifiedId));

@visibleForTesting
bool isTerminalBackgroundNotificationStatus(int statusCode) =>
    statusCode == 401 || statusCode == 403;

@visibleForTesting
bool shouldKeepBackgroundNotificationsEnabled({
  required bool isAndroid,
  required bool enabledInStorage,
  required bool hasAccessToken,
  required bool hasPermission,
}) => isAndroid && enabledInStorage && hasAccessToken && hasPermission;

Future<bool> refreshBackgroundNotificationState() async {
  final enabledInStorage = await _storage.read(key: _enabledKey) == 'true';
  final token = await _storage.read(key: 'access_token');
  final isAndroid = defaultTargetPlatform == TargetPlatform.android;
  final android = isAndroid
      ? _localNotifications
            .resolvePlatformSpecificImplementation<
              AndroidFlutterLocalNotificationsPlugin
            >()
      : null;
  final hasPermission = await android?.areNotificationsEnabled() == true;
  final enabled = shouldKeepBackgroundNotificationsEnabled(
    isAndroid: isAndroid,
    enabledInStorage: enabledInStorage,
    hasAccessToken: token != null && token.isNotEmpty,
    hasPermission: hasPermission,
  );
  if (!enabled && enabledInStorage) {
    await disableBackgroundNotifications();
  }
  return enabled;
}

Future<void> initializeBackgroundNotifications() async {
  const androidSettings = AndroidInitializationSettings('@mipmap/ic_launcher');
  await _localNotifications.initialize(
    settings: const InitializationSettings(android: androidSettings),
  );
  if (defaultTargetPlatform == TargetPlatform.android) {
    await Workmanager().initialize(briefingNotificationDispatcher);
  }
}

Future<BackgroundNotificationEnableResult> enableBackgroundNotifications({
  required String baseUrl,
  required String token,
}) async {
  final isAndroid = defaultTargetPlatform == TargetPlatform.android;
  if (!isAndroid) {
    return BackgroundNotificationEnableResult.unsupportedPlatform;
  }

  final android = _localNotifications
      .resolvePlatformSpecificImplementation<
        AndroidFlutterLocalNotificationsPlugin
      >();
  final permissionGranted = await android?.requestNotificationsPermission();
  final permissionResult = notificationEnablePreflight(
    isAndroid: isAndroid,
    hasPermission: permissionGranted == true,
    baselineLoaded: true,
  );
  if (permissionResult != BackgroundNotificationEnableResult.enabled) {
    return permissionResult;
  }

  // Remember the latest existing briefing so enabling notifications does not
  // announce old content as if it had just arrived.
  final snapshot = await _fetchLatestBriefingSnapshot(baseUrl, token);
  final baselineResult = notificationEnablePreflight(
    isAndroid: isAndroid,
    hasPermission: true,
    baselineLoaded: snapshot.succeeded,
  );
  if (baselineResult != BackgroundNotificationEnableResult.enabled) {
    await _storage.delete(key: _lastBriefingKey);
    await _storage.write(key: _enabledKey, value: 'false');
    await Workmanager().cancelByUniqueName(_notificationTask);
    return baselineResult;
  } else if (snapshot.id != null) {
    await _storage.write(key: _lastBriefingKey, value: snapshot.id);
  } else {
    await _storage.write(key: _lastBriefingKey, value: noPriorBriefingBaseline);
  }
  await _storage.write(key: _enabledKey, value: 'true');
  try {
    await scheduleBackgroundNotifications();
  } catch (_) {
    await _storage.write(key: _enabledKey, value: 'false');
    await _storage.delete(key: _lastBriefingKey);
    await Workmanager().cancelByUniqueName(_notificationTask);
    rethrow;
  }
  return BackgroundNotificationEnableResult.enabled;
}

Future<void> scheduleBackgroundNotifications() async {
  if (defaultTargetPlatform != TargetPlatform.android ||
      await _storage.read(key: _enabledKey) != 'true') {
    return;
  }
  await Workmanager().registerPeriodicTask(
    _notificationTask,
    _notificationTask,
    frequency: const Duration(minutes: 15),
    existingWorkPolicy: ExistingPeriodicWorkPolicy.update,
    constraints: Constraints(networkType: NetworkType.connected),
  );
}

Future<void> disableBackgroundNotifications() async {
  await _storage.write(key: _enabledKey, value: 'false');
  await _storage.delete(key: _lastBriefingKey);
  if (defaultTargetPlatform == TargetPlatform.android) {
    await Workmanager().cancelByUniqueName(_notificationTask);
  }
}

Future<_BriefingSnapshot> _fetchLatestBriefingSnapshot(
  String baseUrl,
  String token,
) async {
  try {
    final uri = Uri.parse(
      '${baseUrl.replaceFirst(RegExp(r'/+$'), '')}/api/v1/briefings?limit=1',
    );
    final response = await http
        .get(
          uri,
          headers: {
            'Accept': 'application/json',
            'Authorization': 'Bearer $token',
          },
        )
        .timeout(const Duration(seconds: 20));
    if (response.statusCode != 200) {
      return const _BriefingSnapshot(succeeded: false);
    }
    return _BriefingSnapshot(
      succeeded: true,
      id: newestBriefingId(jsonDecode(response.body)),
    );
  } catch (_) {
    return const _BriefingSnapshot(succeeded: false);
  }
}

@pragma('vm:entry-point')
void briefingNotificationDispatcher() {
  Workmanager().executeTask((task, inputData) async {
    if (task != _notificationTask ||
        await _storage.read(key: _enabledKey) != 'true') {
      return true;
    }

    final token = await _storage.read(key: 'access_token');
    final baseUrl =
        await _storage.read(key: 'api_url') ?? 'https://api.pulsebrief.cloud';
    if (token == null || token.isEmpty) {
      await disableBackgroundNotifications();
      return true;
    }

    try {
      final android = _localNotifications
          .resolvePlatformSpecificImplementation<
            AndroidFlutterLocalNotificationsPlugin
          >();
      if (await android?.areNotificationsEnabled() == false) {
        await disableBackgroundNotifications();
        return true;
      }

      final uri = Uri.parse(
        '${baseUrl.replaceFirst(RegExp(r'/+$'), '')}/api/v1/briefings?limit=1',
      );
      final response = await http
          .get(
            uri,
            headers: {
              'Accept': 'application/json',
              'Authorization': 'Bearer $token',
            },
          )
          .timeout(const Duration(seconds: 20));
      if (isTerminalBackgroundNotificationStatus(response.statusCode)) {
        await disableBackgroundNotifications();
        return true;
      }
      if (response.statusCode != 200) return false;

      final currentId = newestBriefingId(jsonDecode(response.body));
      if (currentId == null) return true;
      final lastId = await _storage.read(key: _lastBriefingKey);
      if (lastId == null || lastId.isEmpty) {
        await _storage.write(key: _lastBriefingKey, value: currentId);
        return true;
      }
      if (!isNewBriefing(currentId, lastId)) return true;
      if (await _storage.read(key: _enabledKey) != 'true' ||
          await _storage.read(key: 'access_token') != token ||
          await android?.areNotificationsEnabled() == false) {
        return true;
      }

      await _localNotifications.show(
        id: 7201,
        title: _notificationTitle,
        body: '你的新简报已生成，点击打开 PulseBrief 查看。',
        notificationDetails: const NotificationDetails(
          android: AndroidNotificationDetails(
            'briefing_updates',
            '简报更新',
            channelDescription: '有新简报时提醒你打开 PulseBrief 查看。',
            importance: Importance.defaultImportance,
            priority: Priority.defaultPriority,
            visibility: NotificationVisibility.private,
          ),
        ),
      );
      await _storage.write(key: _lastBriefingKey, value: currentId);
      return true;
    } catch (_) {
      return false;
    }
  });
}
