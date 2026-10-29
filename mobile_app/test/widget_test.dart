import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:ai_pulse/main.dart';
import 'package:ai_pulse/admin_feedback_sheet.dart';
import 'package:ai_pulse/background_notifications.dart';

void main() {
  test('release builds default to the public service endpoint', () {
    expect(defaultApiUrl(isRelease: true), 'https://api.pulsebrief.cloud');
  });

  test('debug builds keep the Android emulator endpoint', () {
    expect(defaultApiUrl(isRelease: false), 'http://10.0.2.2:8000');
  });

  test('configured endpoint overrides the build default', () {
    expect(
      defaultApiUrl(isRelease: true, configuredUrl: ' https://example.test/ '),
      'https://example.test/',
    );
  });

  test('administrator controls depend on the server-provided role flag', () {
    expect(userHasAdminAccess({'is_admin': true}), isTrue);
    expect(userHasAdminAccess({'is_admin': false}), isFalse);
    expect(userHasAdminAccess({'email': 'admin@example.test'}), isFalse);
    expect(userHasAdminAccess(null), isFalse);
  });

  testWidgets('admin feedback sheet explains an empty queue', (tester) async {
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: AdminFeedbackSheet(
            items: const [],
            onSave: (id, status, reply) async => {},
            cleanError: (error) => error.toString(),
          ),
        ),
      ),
    );

    expect(find.text('举报与更正处理'), findsOneWidget);
    expect(find.text('当前没有待处理的举报或更正。'), findsOneWidget);
  });

  testWidgets('admin resolution requires a reply and persists the result', (
    tester,
  ) async {
    String? savedId;
    String? savedStatus;
    String? savedReply;
    final item = {
      'id': 'feedback-1',
      'briefing_title': '今日简报',
      'business_date': '2026-10-23',
      'kind': 'correction',
      'detail': '文章日期不正确，请核对来源。',
      'status': 'open',
      'reply': '',
    };
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: AdminFeedbackSheet(
            items: [item],
            cleanError: (error) => error.toString(),
            onSave: (id, status, reply) async {
              savedId = id;
              savedStatus = status;
              savedReply = reply;
              return {...item, 'status': status, 'reply': reply};
            },
          ),
        ),
      ),
    );

    expect(find.text('今日简报'), findsOneWidget);
    expect(find.text('文章日期不正确，请核对来源。'), findsOneWidget);
    await tester.tap(
      find.byKey(const ValueKey('admin-feedback-status-feedback-1')),
    );
    await tester.pumpAndSettle();
    await tester.tap(find.text('已解决').last);
    await tester.pumpAndSettle();
    await tester.ensureVisible(
      find.byKey(const ValueKey('admin-feedback-save-feedback-1')),
    );
    await tester.tap(
      find.byKey(const ValueKey('admin-feedback-save-feedback-1')),
    );
    await tester.pumpAndSettle();

    expect(find.text('完成或不采纳反馈前，请填写处理回复。'), findsOneWidget);
    expect(savedId, isNull);

    await tester.enterText(
      find.byKey(const ValueKey('admin-feedback-reply-feedback-1')),
      '已核实来源并完成日期更正。',
    );
    await tester.ensureVisible(
      find.byKey(const ValueKey('admin-feedback-save-feedback-1')),
    );
    await tester.tap(
      find.byKey(const ValueKey('admin-feedback-save-feedback-1')),
    );
    await tester.pumpAndSettle();

    expect(savedId, 'feedback-1');
    expect(savedStatus, 'resolved');
    expect(savedReply, '已核实来源并完成日期更正。');
    expect(find.textContaining('已解决 · 内容更正'), findsOneWidget);
  });

  test(
    'background notifications only enable after permission and baseline checks',
    () {
      expect(
        notificationEnablePreflight(
          isAndroid: false,
          hasPermission: true,
          baselineLoaded: true,
        ),
        BackgroundNotificationEnableResult.unsupportedPlatform,
      );
      expect(
        notificationEnablePreflight(
          isAndroid: true,
          hasPermission: false,
          baselineLoaded: true,
        ),
        BackgroundNotificationEnableResult.permissionDenied,
      );
      expect(
        notificationEnablePreflight(
          isAndroid: true,
          hasPermission: true,
          baselineLoaded: false,
        ),
        BackgroundNotificationEnableResult.baselineUnavailable,
      );
      expect(
        notificationEnablePreflight(
          isAndroid: true,
          hasPermission: true,
          baselineLoaded: true,
        ),
        BackgroundNotificationEnableResult.enabled,
      );
    },
  );

  test('agent request stages have readable labels', () {
    expect(agentRequestPhaseLabel('tool_selection'), '工具选择');
    expect(agentRequestPhaseLabel('evidence_repair'), '证据修正');
    expect(agentRequestPhaseLabel('future_phase'), '模型请求');
  });

  test(
    'background notification baseline ignores old content and detects new IDs',
    () {
      expect(
        newestBriefingId({
          'items': [
            {'id': 'briefing-1'},
          ],
        }),
        'briefing-1',
      );
      expect(newestBriefingId({'items': []}), isNull);
      expect(isNewBriefing('briefing-1', null), isFalse);
      expect(isNewBriefing('briefing-1', 'briefing-1'), isFalse);
      expect(isNewBriefing('briefing-2', 'briefing-1'), isTrue);
      expect(isNewBriefing('first-briefing', noPriorBriefingBaseline), isTrue);
      expect(isNewBriefing(null, noPriorBriefingBaseline), isFalse);
    },
  );

  test(
    'background notifications stop polling after terminal auth failures',
    () {
      expect(isTerminalBackgroundNotificationStatus(401), isTrue);
      expect(isTerminalBackgroundNotificationStatus(403), isTrue);
      expect(isTerminalBackgroundNotificationStatus(429), isFalse);
      expect(isTerminalBackgroundNotificationStatus(500), isFalse);
    },
  );

  test(
    'background notification state requires Android, token and permission',
    () {
      expect(
        shouldKeepBackgroundNotificationsEnabled(
          isAndroid: true,
          enabledInStorage: true,
          hasAccessToken: true,
          hasPermission: true,
        ),
        isTrue,
      );
      expect(
        shouldKeepBackgroundNotificationsEnabled(
          isAndroid: true,
          enabledInStorage: false,
          hasAccessToken: true,
          hasPermission: true,
        ),
        isFalse,
      );
      expect(
        shouldKeepBackgroundNotificationsEnabled(
          isAndroid: true,
          enabledInStorage: true,
          hasAccessToken: false,
          hasPermission: true,
        ),
        isFalse,
      );
      expect(
        shouldKeepBackgroundNotificationsEnabled(
          isAndroid: true,
          enabledInStorage: true,
          hasAccessToken: true,
          hasPermission: false,
        ),
        isFalse,
      );
      expect(
        shouldKeepBackgroundNotificationsEnabled(
          isAndroid: false,
          enabledInStorage: true,
          hasAccessToken: true,
          hasPermission: true,
        ),
        isFalse,
      );
    },
  );

  testWidgets(
    'welcome screen offers account sign-in without assuming demo mode',
    (tester) async {
      await tester.pumpWidget(const AIPulseApp());
      await tester.pump();

      expect(find.text('每天几分钟，\n跟上重要新闻。'), findsOneWidget);
      expect(find.text('邮箱验证码登录 / 注册'), findsOneWidget);
      expect(find.text('验证并登录 / 注册'), findsOneWidget);
      expect(find.text('进入本机体验模式'), findsNothing);
      expect(find.text('配置后端地址'), findsOneWidget);
    },
  );

  testWidgets('configured subscription does not show the setup empty state', (
    tester,
  ) async {
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: EmptyBriefingCard(
            hasSubscription: true,
            onEditPreferences: () {},
          ),
        ),
      ),
    );

    expect(find.text('今天还没有简报'), findsOneWidget);
    expect(find.text('你的简报还在等一个关注方向'), findsNothing);
    expect(find.text('关注偏好已保存。你可以尝试生成预览，或稍后再来查看。'), findsOneWidget);
  });

  testWidgets('unconfigured subscription offers the setup action', (
    tester,
  ) async {
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: EmptyBriefingCard(
            hasSubscription: false,
            onEditPreferences: () {},
          ),
        ),
      ),
    );

    expect(find.text('你的简报还在等一个关注方向'), findsOneWidget);
    expect(find.text('设置订阅偏好'), findsOneWidget);
  });

  testWidgets('notification tile shows an unread briefing and opens on tap', (
    tester,
  ) async {
    var opened = false;
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: BriefingNotificationTile(
            briefing: {
              'title': 'AI 每日简报（2 条）',
              'business_date': '2026-10-18',
              'items': [{}, {}],
              'is_read': false,
            },
            onTap: () => opened = true,
          ),
        ),
      ),
    );

    expect(find.text('AI 每日简报（2 条）'), findsOneWidget);
    expect(find.text('2026-10-18 · 2 条'), findsOneWidget);
    expect(find.byIcon(Icons.notifications_active_outlined), findsOneWidget);
    await tester.tap(find.text('AI 每日简报（2 条）'));
    expect(opened, isTrue);
  });

  testWidgets('read notification tile is marked as read', (tester) async {
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: BriefingNotificationTile(
            briefing: {
              'title': '已读简报',
              'business_date': '2026-10-17',
              'items': [{}],
              'is_read': true,
            },
            onTap: () {},
          ),
        ),
      ),
    );

    expect(find.byIcon(Icons.article_outlined), findsOneWidget);
    expect(find.byIcon(Icons.done_all), findsOneWidget);
  });

  testWidgets('licensed article summaries show the author and reuse credits', (
    tester,
  ) async {
    var openedLicense = '';
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Text('作者：Daria Dergacheva'),
              ArticleAttribution(
                item: {
                  'republish_credit': 'Originally published by Global Voices. Reused under CC BY 3.0.',
                  'change_notice': '此内容为 AI 生成的中文摘要，已对原文进行概括和翻译。',
                  'license_name': 'CC BY 3.0',
                  'license_url': 'https://creativecommons.org/licenses/by/3.0/',
                },
                onOpenLink: (value) => openedLicense = value,
              ),
            ],
          ),
        ),
      ),
    );

    expect(find.text('作者：Daria Dergacheva'), findsOneWidget);
    expect(
      find.text(
        'Originally published by Global Voices. Reused under CC BY 3.0.',
      ),
      findsOneWidget,
    );
    expect(find.text('此内容为 AI 生成的中文摘要，已对原文进行概括和翻译。'), findsOneWidget);
    await tester.tap(find.text('许可：CC BY 3.0'));
    expect(openedLicense, 'https://creativecommons.org/licenses/by/3.0/');
  });

  testWidgets('article source and original link are shown before the summary', (
    tester,
  ) async {
    var openedSource = '';
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              ArticleSourceHeader(
                sourceName: '中国新闻网',
                sourceUrl: 'https://news.example.test/story/1',
                onOpenSource: (value) => openedSource = value,
              ),
              const Text('事件总体摘要及关键事实。'),
            ],
          ),
        ),
      ),
    );

    final sourceY = tester.getTopLeft(find.text('来源：中国新闻网')).dy;
    final summaryY = tester.getTopLeft(find.text('事件总体摘要及关键事实。')).dy;
    expect(sourceY, lessThan(summaryY));
    await tester.tap(find.text('打开原文'));
    expect(openedSource, 'https://news.example.test/story/1');
  });
}
