import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:http/http.dart' as http;
import 'package:url_launcher/url_launcher.dart';

import 'admin_feedback_sheet.dart';
import 'background_notifications.dart';

void main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await initializeBackgroundNotifications();
  runApp(const AIPulseApp());
}

const _ink = Color(0xFF17322D);
const _green = Color(0xFF2F6B58);
const _paper = Color(0xFFF6F5F0);
const _storage = FlutterSecureStorage();

String defaultApiUrl({required bool isRelease, String? configuredUrl}) {
  final override = configuredUrl?.trim();
  if (override != null && override.isNotEmpty) return override;
  return isRelease ? 'https://api.pulsebrief.cloud' : 'http://10.0.2.2:8000';
}

String agentRequestPhaseLabel(Object? phase) => switch (phase?.toString()) {
  'tool_selection' => '工具选择',
  'final_response' => '生成结果',
  'format_repair' => '格式修正',
  'topic_coverage_repair' => '主题补全',
  'summary_length_repair' => '摘要精简',
  'evidence_repair' => '证据修正',
  _ => '模型请求',
};

bool userHasAdminAccess(Map<String, dynamic>? user) =>
    user?['is_admin'] == true;

class AIPulseApp extends StatelessWidget {
  const AIPulseApp({super.key});

  @override
  Widget build(BuildContext context) => MaterialApp(
    title: 'PulseBrief',
    debugShowCheckedModeBanner: false,
    theme: ThemeData(
      colorScheme: ColorScheme.fromSeed(seedColor: _green),
      scaffoldBackgroundColor: _paper,
      useMaterial3: true,
      appBarTheme: const AppBarTheme(
        backgroundColor: _paper,
        foregroundColor: _ink,
      ),
      cardTheme: CardThemeData(
        color: Colors.white,
        elevation: 0,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(18)),
      ),
    ),
    home: const HomeShell(),
  );
}

class PulseApi {
  PulseApi(this.baseUrl, this.token);
  final String baseUrl;
  final String? token;

  Future<dynamic> call(
    String path, {
    String method = 'GET',
    Object? body,
  }) async {
    final uri = Uri.parse(
      '${baseUrl.replaceFirst(RegExp(r'/+$'), '')}/api/v1$path',
    );
    final headers = <String, String>{'Accept': 'application/json'};
    if (token != null) headers['Authorization'] = 'Bearer $token';
    if (body != null) headers['Content-Type'] = 'application/json';
    final request = http.Request(method, uri)..headers.addAll(headers);
    if (body != null) request.body = jsonEncode(body);
    final streamed = await request.send().timeout(const Duration(seconds: 30));
    final response = await http.Response.fromStream(streamed);
    final payload = response.body.isEmpty ? null : jsonDecode(response.body);
    if (response.statusCode < 200 || response.statusCode >= 300) {
      final detail = payload is Map ? payload['detail'] : null;
      throw Exception(
        detail is String ? detail : '请求失败（${response.statusCode}），请检查服务地址。',
      );
    }
    return payload;
  }
}

class HomeShell extends StatefulWidget {
  const HomeShell({super.key});
  @override
  State<HomeShell> createState() => _HomeShellState();
}

class _HomeShellState extends State<HomeShell> with WidgetsBindingObserver {
  String _baseUrl = defaultApiUrl(
    isRelease: kReleaseMode,
    configuredUrl: const String.fromEnvironment('PULSEBRIEF_API_URL'),
  );
  String? _token;
  Map<String, dynamic>? _user;
  Map<String, dynamic>? _subscription;
  List<dynamic> _briefings = [];
  int _tab = 0;
  bool _busy = false;
  bool _localLoginEnabled = false;
  bool _backgroundNotificationsEnabled = false;
  String? _activeTask;
  String? _error;
  final _emailController = TextEditingController();
  final _codeController = TextEditingController();
  Timer? _codeCooldownTimer;
  bool _codeSent = false;
  int _codeCooldown = 0;

  PulseApi get _api => PulseApi(_baseUrl, _token);
  bool get _signedIn => _token != null;
  int get _unreadBriefingCount =>
      _briefings.where((item) => item is Map && item['is_read'] != true).length;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _restore();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) {
      unawaited(_refreshBackgroundNotificationState());
    }
  }

  Future<void> _refreshBackgroundNotificationState() async {
    final enabled = await refreshBackgroundNotificationState();
    if (!mounted || enabled == _backgroundNotificationsEnabled) return;
    setState(() => _backgroundNotificationsEnabled = enabled);
  }

  Future<void> _restore() async {
    final url = await _storage.read(key: 'api_url');
    final token = await _storage.read(key: 'access_token');
    final pendingTask = await _storage.read(key: 'pending_task_id');
    final backgroundNotificationsEnabled =
        await refreshBackgroundNotificationState();
    if (!mounted) return;
    setState(() {
      if (url != null) _baseUrl = url;
      _token = token;
      _activeTask = pendingTask;
      _busy = pendingTask != null;
      _backgroundNotificationsEnabled =
          backgroundNotificationsEnabled && token != null;
    });
    if (backgroundNotificationsEnabled && token != null) {
      await scheduleBackgroundNotifications();
    } else if (backgroundNotificationsEnabled) {
      await disableBackgroundNotifications();
    }
    unawaited(_refreshLoginOptions());
    if (token != null) {
      await _loadAll();
      if (pendingTask != null) unawaited(_watchTask(pendingTask));
    }
  }

  Future<void> _refreshLoginOptions() async {
    try {
      final health = await PulseApi(_baseUrl, null).call('/health');
      if (!mounted) return;
      setState(
        () => _localLoginEnabled = health['local_login_enabled'] == true,
      );
    } catch (_) {
      if (!mounted) return;
      setState(() => _localLoginEnabled = false);
    }
  }

  Future<void> _loadAll() async {
    setState(() => _error = null);
    try {
      final results = await Future.wait([
        _api.call('/me'),
        _api.call('/subscription'),
        _api.call('/briefings?limit=30'),
      ]);
      if (!mounted) return;
      setState(() {
        _user = Map<String, dynamic>.from(results[0] as Map);
        _subscription = Map<String, dynamic>.from(results[1] as Map);
        _briefings = (results[2]['items'] as List<dynamic>? ?? []);
      });
    } catch (e) {
      if (!mounted) return;
      setState(() => _error = _cleanError(e));
    }
  }

  Future<void> _login() async {
    setState(() => _busy = true);
    try {
      final data = await _api.call('/auth/demo', method: 'POST');
      await _acceptSession(data);
    } catch (e) {
      _showError('演示登录不可用：${_cleanError(e)}\n请确认后端正在运行。');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _sendLoginCode() async {
    final email = _emailController.text.trim();
    if (!RegExp(r'^[^@\s]+@[^@\s]+\.[^@\s]+$').hasMatch(email)) {
      _showError('请输入有效的邮箱地址。');
      return;
    }
    setState(() => _busy = true);
    try {
      await _api.call(
        '/auth/send-code',
        method: 'POST',
        body: {'email': email},
      );
      if (!mounted) return;
      setState(() {
        _codeSent = true;
        _codeCooldown = 60;
      });
      _codeCooldownTimer?.cancel();
      _codeCooldownTimer = Timer.periodic(const Duration(seconds: 1), (timer) {
        if (!mounted || _codeCooldown <= 1) {
          timer.cancel();
          if (mounted) setState(() => _codeCooldown = 0);
          return;
        }
        setState(() => _codeCooldown--);
      });
      _toast('验证码已发送，请检查邮箱。');
    } catch (e) {
      final message = _cleanError(e);
      _showError(
        message.contains('temporarily unavailable')
            ? _localLoginEnabled
                  ? '邮箱验证码服务暂不可用；本机测试可使用本机体验登录。'
                  : '邮箱验证码服务暂不可用，请稍后重试。'
            : message,
      );
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _verifyLoginCode() async {
    final email = _emailController.text.trim();
    final code = _codeController.text.trim();
    if (!RegExp(r'^\d{6}$').hasMatch(code)) {
      _showError('请输入邮件中的 6 位验证码。');
      return;
    }
    setState(() => _busy = true);
    try {
      final data = await _api.call(
        '/auth/verify-code',
        method: 'POST',
        body: {'email': email, 'code': code},
      );
      await _acceptSession(data);
    } catch (e) {
      _showError(_cleanError(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _acceptSession(dynamic data) async {
    final token = data['access_token'] as String;
    final existingToken = await _storage.read(key: 'access_token');
    if (existingToken != null && existingToken != token) {
      await disableBackgroundNotifications();
      if (mounted) setState(() => _backgroundNotificationsEnabled = false);
    }
    await _storage.write(key: 'access_token', value: token);
    if (!mounted) return;
    setState(() {
      _token = token;
      _user = Map<String, dynamic>.from(data['user'] as Map);
    });
    await _loadAll();
  }

  Future<void> _logout() async {
    await disableBackgroundNotifications();
    await _storage.delete(key: 'access_token');
    await _storage.delete(key: 'pending_task_id');
    if (!mounted) return;
    setState(() {
      _token = null;
      _user = null;
      _subscription = null;
      _briefings = [];
      _activeTask = null;
      _backgroundNotificationsEnabled = false;
    });
    await _refreshLoginOptions();
  }

  Future<void> _deleteAccount() async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: const Text('注销并删除账户？'),
        content: const Text('这会删除账户、订阅偏好、简报和阅读状态，且无法撤销。'),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext, false),
            child: const Text('保留账户'),
          ),
          FilledButton.tonal(
            onPressed: () => Navigator.pop(dialogContext, true),
            child: const Text('确认注销'),
          ),
        ],
      ),
    );
    if (confirmed != true) return;
    try {
      await _api.call('/me', method: 'DELETE');
      await _logout();
      _toast('账户及关联数据已删除');
    } catch (e) {
      _showError(_cleanError(e));
    }
  }

  Future<void> _exportMyData() async {
    try {
      final payload = await _api.call('/me/export');
      final encoded = const JsonEncoder.withIndent('  ').convert(payload);
      if (!mounted) return;
      final shouldCopy = await showDialog<bool>(
        context: context,
        builder: (dialogContext) => AlertDialog(
          title: const Text('复制个人数据副本'),
          content: const Text(
            '副本包含账户资料、订阅偏好、简报历史、阅读收藏、任务记录、反馈和投递记录。'
            '登录令牌和邮箱验证码等安全凭据不会导出。复制后请注意剪贴板可能被设备上的其他应用读取。',
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(dialogContext, false),
              child: const Text('取消'),
            ),
            FilledButton(
              onPressed: () => Navigator.pop(dialogContext, true),
              child: const Text('复制 JSON'),
            ),
          ],
        ),
      );
      if (shouldCopy != true || !mounted) return;
      await Clipboard.setData(ClipboardData(text: encoded));
      _toast('个人数据副本已复制到剪贴板');
    } catch (e) {
      _showError(_cleanError(e));
    }
  }

  Future<void> _saveSubscription(Map<String, dynamic> values) async {
    setState(() => _busy = true);
    try {
      final data = await _api.call(
        '/subscription',
        method: 'PUT',
        body: values,
      );
      if (!mounted) return;
      setState(() => _subscription = Map<String, dynamic>.from(data as Map));
      _toast('订阅偏好已保存');
    } catch (e) {
      _showError(_cleanError(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _generate() async {
    setState(() => _busy = true);
    try {
      final task = await _api.call(
        '/briefing-tasks',
        method: 'POST',
        body: {'kind': 'preview'},
      );
      final id = task['id'] as String;
      await _storage.write(key: 'pending_task_id', value: id);
      setState(() {
        _activeTask = id;
      });
      _toast('已开始整理，完成后会显示在今日简报');
      await _watchTask(id);
    } catch (e) {
      _showError(_cleanError(e));
    } finally {
      if (mounted) {
        setState(() => _busy = false);
      }
    }
  }

  Future<void> _watchTask(String id) async {
    var attempts = 0;
    try {
      while (mounted && _signedIn) {
        await Future<void>.delayed(Duration(seconds: attempts++ < 5 ? 2 : 5));
        final current = await _api.call(
          '/briefing-tasks/${Uri.encodeComponent(id)}',
        );
        final status = current['status'] as String? ?? '';
        if (status == 'published' || status == 'no_match') {
          await _storage.delete(key: 'pending_task_id');
          if (mounted) {
            setState(() {
              _activeTask = null;
              _busy = false;
            });
            await _loadAll();
            final stopReason = current['stop_reason']?.toString();
            _toast(
              status == 'published'
                  ? '今日简报已更新'
                  : stopReason == 'evidence_repair_failed'
                  ? '候选内容未能完成证据核验，本次没有发布简报。'
                  : '已完成整理，当前没有符合偏好的新闻',
            );
          }
          return;
        }
        if (status == 'failed' || status == 'cancelled') {
          await _storage.delete(key: 'pending_task_id');
          if (mounted) {
            setState(() {
              _activeTask = null;
              _busy = false;
            });
            _showError(
              current['error_message']?.toString().isNotEmpty == true
                  ? current['error_message'].toString()
                  : '简报生成失败，请稍后重试。',
            );
          }
          return;
        }
      }
    } catch (e) {
      if (mounted) {
        setState(() => _busy = false);
        _showError('暂时无法查询任务进度，重新打开 App 后会继续查询。${_cleanError(e)}');
      }
    }
  }

  Future<void> _updateBriefing(
    String id, {
    bool? read,
    bool? bookmarked,
  }) async {
    try {
      await _api.call(
        '/briefings/${Uri.encodeComponent(id)}/state',
        method: 'PUT',
        body: {
          ...?(read == null ? null : {'is_read': read}),
          ...?(bookmarked == null ? null : {'is_bookmarked': bookmarked}),
        },
      );
      await _loadAll();
    } catch (e) {
      _showError(_cleanError(e));
    }
  }

  Future<void> _openSettings() async {
    final controller = TextEditingController(text: _baseUrl);
    final result = await showDialog<String>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('服务连接'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Text(
              '填写运行后端电脑的地址。模拟器可用默认地址；真机请填写电脑局域网 IP，例如 http://192.168.1.20:8000。',
            ),
            const SizedBox(height: 14),
            TextField(
              controller: controller,
              keyboardType: TextInputType.url,
              decoration: const InputDecoration(labelText: '后端地址'),
            ),
          ],
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context),
            child: const Text('取消'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, controller.text.trim()),
            child: const Text('保存'),
          ),
        ],
      ),
    );
    if (result == null || result.isEmpty) return;
    final normalized = result.replaceFirst(RegExp(r'/+$'), '');
    await _storage.write(key: 'api_url', value: normalized);
    if (!mounted) return;
    setState(() => _baseUrl = normalized);
    if (_signedIn) {
      await _loadAll();
    } else {
      await _refreshLoginOptions();
    }
    _toast('服务地址已保存');
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _codeCooldownTimer?.cancel();
    _emailController.dispose();
    _codeController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    if (!_signedIn) return _welcome();
    final pages = [
      _today(),
      _notifications(),
      _history(),
      _preferences(),
      _profile(),
    ];
    return Scaffold(
      appBar: AppBar(
        title: Row(
          children: [
            const Text(
              'PulseBrief',
              style: TextStyle(fontWeight: FontWeight.w800),
            ),
            const SizedBox(width: 8),
            _tag('每日新闻简报'),
          ],
        ),
        actions: [
          IconButton(
            tooltip: '刷新',
            onPressed: _loadAll,
            icon: const Icon(Icons.refresh),
          ),
        ],
      ),
      body: SafeArea(child: _error != null ? _errorView() : pages[_tab]),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _tab,
        onDestinationSelected: (value) => setState(() => _tab = value),
        destinations: [
          const NavigationDestination(
            icon: Icon(Icons.wb_sunny_outlined),
            selectedIcon: Icon(Icons.wb_sunny),
            label: '今日',
          ),
          NavigationDestination(
            icon: Badge(
              isLabelVisible: _unreadBriefingCount > 0,
              label: Text('$_unreadBriefingCount'),
              child: Icon(Icons.notifications_none),
            ),
            selectedIcon: Badge(
              isLabelVisible: _unreadBriefingCount > 0,
              label: Text('$_unreadBriefingCount'),
              child: Icon(Icons.notifications),
            ),
            label: '通知',
          ),
          const NavigationDestination(icon: Icon(Icons.history), label: '历史'),
          const NavigationDestination(icon: Icon(Icons.tune), label: '订阅'),
          const NavigationDestination(
            icon: Icon(Icons.person_outline),
            label: '我的',
          ),
        ],
      ),
    );
  }

  Widget _welcome() => Scaffold(
    body: SafeArea(
      child: SingleChildScrollView(
        padding: const EdgeInsets.all(26),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const SizedBox(height: 34),
            Container(
              width: 68,
              height: 68,
              decoration: BoxDecoration(
                color: _green,
                borderRadius: BorderRadius.circular(22),
              ),
              child: const Icon(Icons.bolt, color: Colors.white, size: 38),
            ),
            const SizedBox(height: 24),
            const Text(
              '每天几分钟，\n跟上重要新闻。',
              style: TextStyle(
                fontSize: 34,
                height: 1.2,
                fontWeight: FontWeight.w800,
                color: _ink,
              ),
            ),
            const SizedBox(height: 14),
            const Text(
              '根据你关注的话题整理新闻，附上原文来源，方便核对。',
              style: TextStyle(
                fontSize: 16,
                height: 1.6,
                color: Colors.black54,
              ),
            ),
            const SizedBox(height: 28),
            const Text(
              '邮箱验证码登录 / 注册',
              style: TextStyle(fontSize: 18, fontWeight: FontWeight.w700),
            ),
            const SizedBox(height: 10),
            TextField(
              controller: _emailController,
              keyboardType: TextInputType.emailAddress,
              textInputAction: TextInputAction.next,
              autofillHints: const [AutofillHints.email],
              decoration: const InputDecoration(
                labelText: '邮箱地址',
                prefixIcon: Icon(Icons.email_outlined),
                border: OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 10),
            Row(
              children: [
                Expanded(
                  child: TextField(
                    controller: _codeController,
                    keyboardType: TextInputType.number,
                    textInputAction: TextInputAction.done,
                    maxLength: 6,
                    decoration: const InputDecoration(
                      labelText: '6 位验证码',
                      counterText: '',
                      prefixIcon: Icon(Icons.password),
                      border: OutlineInputBorder(),
                    ),
                  ),
                ),
                const SizedBox(width: 10),
                OutlinedButton(
                  onPressed: _busy || _codeCooldown > 0 ? null : _sendLoginCode,
                  child: Text(
                    _codeCooldown > 0 ? '重新发送 ${_codeCooldown}s' : '发送验证码',
                  ),
                ),
              ],
            ),
            SizedBox(
              width: double.infinity,
              child: FilledButton(
                onPressed: _busy ? null : _verifyLoginCode,
                child: _busy
                    ? const SizedBox(
                        width: 18,
                        height: 18,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      )
                    : const Text('验证并登录 / 注册'),
              ),
            ),
            if (_codeSent) ...[
              const SizedBox(height: 8),
              const Text(
                '验证码已发送，10 分钟内有效。',
                style: TextStyle(fontSize: 12, color: _green),
              ),
            ],
            const SizedBox(height: 8),
            const Text(
              '首次验证后会自动创建账户。验证码登录仅在后端邮件服务配置完成后可用。',
              style: TextStyle(fontSize: 12, color: Colors.black54),
            ),
            if (_localLoginEnabled) ...[
              const SizedBox(height: 22),
              SizedBox(
                width: double.infinity,
                child: OutlinedButton.icon(
                  onPressed: _busy ? null : _login,
                  icon: _busy
                      ? const SizedBox(
                          width: 18,
                          height: 18,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Icon(Icons.arrow_forward),
                  label: const Text('进入本机体验模式'),
                ),
              ),
            ],
            TextButton(onPressed: _openSettings, child: const Text('配置后端地址')),
            if (_localLoginEnabled) ...[
              const SizedBox(height: 8),
              const Text(
                '体验模式只适用于开发环境；正式环境会关闭此入口。',
                textAlign: TextAlign.center,
                style: TextStyle(fontSize: 12, color: Colors.black45),
              ),
            ],
            const SizedBox(height: 28),
          ],
        ),
      ),
    ),
  );

  Widget _today() {
    final today = _briefings.isEmpty
        ? null
        : Map<String, dynamic>.from(_briefings.first as Map);
    return RefreshIndicator(
      onRefresh: _loadAll,
      child: ListView(
        padding: const EdgeInsets.fromLTRB(18, 8, 18, 28),
        children: [
          Text(
            _dateLabel(),
            style: const TextStyle(
              color: Colors.black54,
              fontWeight: FontWeight.w600,
            ),
          ),
          const SizedBox(height: 10),
          Container(
            margin: const EdgeInsets.only(bottom: 12),
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: const Color(0xFFE8F1EB),
              borderRadius: BorderRadius.circular(14),
            ),
            child: const Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Icon(Icons.info_outline, color: _green, size: 19),
                SizedBox(width: 8),
                Expanded(
                  child: Text(
                    '当前简报只覆盖已接入且获准使用的来源，国内新闻覆盖尚不完整。没有匹配结果不代表当天没有相关新闻；重要信息请查看原文。',
                    style: TextStyle(
                      color: _ink,
                      fontSize: 12,
                      height: 1.45,
                    ),
                  ),
                ),
              ],
            ),
          ),
          const SizedBox(height: 12),
          Text(
            today == null ? '今天的新闻简报' : (today['title'] as String? ?? '今日新闻简报'),
            style: const TextStyle(
              fontSize: 26,
              fontWeight: FontWeight.w800,
              color: _ink,
            ),
          ),
          const SizedBox(height: 6),
          Text(
            today == null
                ? '按你的关注方向，整理值得了解的新进展。'
                : (today['summary'] as String? ?? ''),
            style: const TextStyle(height: 1.55, color: Colors.black54),
          ),
          const SizedBox(height: 18),
          if (_activeTask != null)
            Card(
              child: const ListTile(
                leading: CircularProgressIndicator(),
                title: Text('正在整理简报'),
                subtitle: Text('通常需要几十秒，请稍候。'),
              ),
            ),
          if (today == null)
            EmptyBriefingCard(
              hasSubscription: _subscription?['configured'] == true,
              onEditPreferences: () => setState(() => _tab = 3),
            )
          else
            ...((today['items'] as List<dynamic>? ?? []).map(
              (item) =>
                  _storyCard(Map<String, dynamic>.from(item as Map), today),
            )),
          const SizedBox(height: 8),
          FilledButton.icon(
            onPressed: _busy ? null : _generate,
            icon: const Icon(Icons.auto_awesome),
            label: Text(_busy ? '正在整理…' : '现在生成一份简报'),
            style: FilledButton.styleFrom(
              minimumSize: const Size.fromHeight(50),
            ),
          ),
          const SizedBox(height: 10),
          const Text(
            '仅在你主动点击时生成预览；每日自动整理会按已保存的订阅时间运行。',
            textAlign: TextAlign.center,
            style: TextStyle(fontSize: 12, color: Colors.black45),
          ),
        ],
      ),
    );
  }

  Widget _storyCard(Map<String, dynamic> story, Map<String, dynamic> briefing) {
    final id = briefing['id']?.toString() ?? '';
    final saved = briefing['is_bookmarked'] == true;
    final sourceUrl = story['source_url'] ?? story['url'];
    return Card(
      margin: const EdgeInsets.only(bottom: 12),
      child: Padding(
        padding: const EdgeInsets.all(17),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                _tag(
                  _sourceDisplayName(
                    story['source_name'] ?? story['source_id'],
                  ),
                ),
                const SizedBox(width: 6),
                _tag(story['content_type'] == 'news' ? '新闻' : '专题文章'),
                const Spacer(),
                IconButton(
                  visualDensity: VisualDensity.compact,
                  tooltip: saved ? '取消收藏' : '收藏',
                  onPressed: () => _updateBriefing(id, bookmarked: !saved),
                  icon: Icon(
                    saved ? Icons.bookmark : Icons.bookmark_border,
                    color: _green,
                  ),
                ),
              ],
            ),
            Text(
              story['title']?.toString() ?? '未命名新闻',
              style: const TextStyle(
                fontSize: 18,
                height: 1.35,
                fontWeight: FontWeight.w700,
                color: _ink,
              ),
            ),
            if ((story['original_title'] ?? '').toString().isNotEmpty &&
                story['original_title'] != story['title'])
              Padding(
                padding: const EdgeInsets.only(top: 4),
                child: Text(
                  '原文：${story['original_title']}',
                  style: const TextStyle(fontSize: 12, color: Colors.black54),
                ),
              ),
            if ((story['author'] ?? '').toString().trim().isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(top: 4),
                child: Text(
                  '作者：${story['author']}',
                  style: const TextStyle(fontSize: 12, color: Colors.black54),
                ),
              ),
            const SizedBox(height: 8),
            Text(
              story['summary']?.toString() ?? '',
              style: const TextStyle(height: 1.55, color: Colors.black87),
            ),
            ArticleAttribution(item: story, onOpenLink: _openSource),
            if (sourceUrl is String && sourceUrl.startsWith('https://'))
              Align(
                alignment: Alignment.centerLeft,
                child: TextButton.icon(
                  onPressed: () => _openSource(sourceUrl),
                  icon: const Icon(Icons.open_in_new, size: 16),
                  label: const Text('查看原文来源'),
                ),
              ),
          ],
        ),
      ),
    );
  }

  Widget _history() => RefreshIndicator(
    onRefresh: _loadAll,
    child: _briefings.isEmpty
        ? ListView(
            children: const [
              SizedBox(height: 180),
              Center(child: Text('还没有历史简报')),
            ],
          )
        : ListView(
            padding: const EdgeInsets.all(16),
            children: [
              const Text(
                '简报历史',
                style: TextStyle(
                  fontSize: 25,
                  fontWeight: FontWeight.w800,
                  color: _ink,
                ),
              ),
              const SizedBox(height: 12),
              ..._briefings.map((raw) {
                final b = Map<String, dynamic>.from(raw as Map);
                return Card(
                  child: ListTile(
                    leading: const CircleAvatar(
                      backgroundColor: Color(0xFFE8F0EA),
                      child: Icon(Icons.article_outlined, color: _green),
                    ),
                    title: Text(
                      b['title']?.toString() ?? '每日简报',
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                    ),
                    subtitle: Text(
                      '${b['business_date'] ?? ''} · ${(b['items'] as List? ?? []).length} 条',
                    ),
                    trailing: Icon(
                      b['is_read'] == true
                          ? Icons.done_all
                          : Icons.chevron_right,
                    ),
                    onTap: () => _showBriefing(b),
                  ),
                );
              }),
            ],
          ),
  );

  Widget _notifications() => RefreshIndicator(
    onRefresh: _loadAll,
    child: _briefings.isEmpty
        ? ListView(
            padding: const EdgeInsets.all(24),
            children: const [
              SizedBox(height: 150),
              Icon(Icons.notifications_none, size: 48, color: _green),
              SizedBox(height: 12),
              Center(
                child: Text(
                  '暂时还没有简报通知',
                  style: TextStyle(fontWeight: FontWeight.w700, fontSize: 18),
                ),
              ),
              SizedBox(height: 8),
              Center(
                child: Text(
                  '每日简报发布后会保存在这里。下拉刷新可同步最新内容。',
                  textAlign: TextAlign.center,
                  style: TextStyle(color: Colors.black54),
                ),
              ),
            ],
          )
        : ListView(
            padding: const EdgeInsets.all(16),
            children: [
              const Text(
                '通知中心',
                style: TextStyle(
                  fontSize: 25,
                  fontWeight: FontWeight.w800,
                  color: _ink,
                ),
              ),
              const SizedBox(height: 4),
              Text(
                _unreadBriefingCount == 0
                    ? '简报已全部读完。'
                    : '有 $_unreadBriefingCount 条未读简报。打开后会自动标记为已读。',
                style: const TextStyle(color: Colors.black54),
              ),
              const SizedBox(height: 12),
              ..._briefings.map((raw) {
                final briefing = Map<String, dynamic>.from(raw as Map);
                return BriefingNotificationTile(
                  briefing: briefing,
                  onTap: () => _showBriefing(briefing),
                );
              }),
              const Padding(
                padding: EdgeInsets.symmetric(vertical: 10),
                child: Text(
                  '简报保存在账户中；可在“我的”中开启本机系统通知。',
                  textAlign: TextAlign.center,
                  style: TextStyle(fontSize: 12, color: Colors.black45),
                ),
              ),
            ],
          ),
  );

  Widget _preferences() {
    if (_subscription == null || _subscription?['configured'] != true) {
      return ListView(
        padding: const EdgeInsets.all(20),
        children: [
          const Text(
            '订阅偏好',
            style: TextStyle(
              fontSize: 25,
              fontWeight: FontWeight.w800,
              color: _ink,
            ),
          ),
          const SizedBox(height: 16),
          const Text('尚未设置订阅。'),
          const SizedBox(height: 12),
          FilledButton(
            onPressed: () => _editPreferences(),
            child: const Text('开始设置'),
          ),
        ],
      );
    }
    final sub = _subscription!;
    return ListView(
      padding: const EdgeInsets.all(18),
      children: [
        const Text(
          '订阅偏好',
          style: TextStyle(
            fontSize: 25,
            fontWeight: FontWeight.w800,
            color: _ink,
          ),
        ),
        const SizedBox(height: 4),
        const Text(
          '选择你关心的方向，系统按设定时间整理。',
          style: TextStyle(color: Colors.black54),
        ),
        const SizedBox(height: 14),
        Card(
          child: Column(
            children: [
              ListTile(
                title: const Text('关注话题'),
                subtitle: Text(
                  (sub['topics'] as List? ?? []).join('、').isEmpty
                      ? '未设置'
                      : (sub['topics'] as List).join('、'),
                ),
              ),
              ListTile(
                title: const Text('关注关键词'),
                subtitle: Text(
                  (sub['keywords'] as List? ?? []).join('、').isEmpty
                      ? '未设置'
                      : (sub['keywords'] as List).join('、'),
                ),
              ),
              ListTile(
                title: const Text('排除关键词'),
                subtitle: Text(
                  (sub['excluded_keywords'] as List? ?? []).join('、').isEmpty
                      ? '无'
                      : (sub['excluded_keywords'] as List).join('、'),
                ),
              ),
              ListTile(
                title: const Text('每日整理时间'),
                subtitle: Text(sub['send_time']?.toString() ?? '08:00'),
                trailing: Switch(
                  value: sub['enabled'] == true,
                  onChanged: (v) {
                    final copy = Map<String, dynamic>.from(sub);
                    copy['enabled'] = v;
                    _saveSubscription(copy);
                  },
                ),
              ),
              ListTile(
                title: const Text('简报长度'),
                subtitle: Text(sub['detail_level'] == 'detailed' ? '详细' : '简洁'),
                trailing: Text('${sub['item_limit'] ?? 10} 条'),
              ),
            ],
          ),
        ),
        const SizedBox(height: 12),
        OutlinedButton.icon(
          onPressed: _editPreferences,
          icon: const Icon(Icons.edit_outlined),
          label: const Text('编辑偏好'),
        ),
      ],
    );
  }

  Future<void> _editPreferences() async {
    final old = _subscription ?? {};
    final topics = TextEditingController(text: _joined(old['topics']));
    final keywords = TextEditingController(text: _joined(old['keywords']));
    final excluded = TextEditingController(
      text: _joined(old['excluded_keywords']),
    );
    bool enabled = old['enabled'] != false;
    bool matchAll = old['match_all'] == true;
    String detail = old['detail_level']?.toString() ?? 'detailed';
    int limit = (old['item_limit'] as num?)?.toInt() ?? 10;
    final result = await showDialog<Map<String, dynamic>>(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, setDialog) => AlertDialog(
          title: const Text('编辑订阅偏好'),
          content: SizedBox(
            width: 420,
            child: SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  _termField(topics, '关注话题', '如：国内新闻、财经、体育、大模型'),
                  _termField(keywords, '包含关键词', '用中文逗号分隔'),
                  _termField(excluded, '排除关键词', '用中文逗号分隔'),
                  SwitchListTile(
                    contentPadding: EdgeInsets.zero,
                    title: const Text('所有关键词都需匹配'),
                    value: matchAll,
                    onChanged: (v) => setDialog(() => matchAll = v),
                  ),
                  DropdownButtonFormField<int>(
                    initialValue: limit,
                    decoration: const InputDecoration(labelText: '每天最多几条'),
                    items: const [5, 10, 15]
                        .map(
                          (v) =>
                              DropdownMenuItem(value: v, child: Text('$v 条')),
                        )
                        .toList(),
                    onChanged: (v) => setDialog(() => limit = v ?? 10),
                  ),
                  DropdownButtonFormField<String>(
                    initialValue: detail,
                    decoration: const InputDecoration(labelText: '摘要长度'),
                    items: const [
                      DropdownMenuItem(value: 'concise', child: Text('简洁')),
                      DropdownMenuItem(value: 'detailed', child: Text('详细')),
                    ],
                    onChanged: (v) => setDialog(() => detail = v ?? 'detailed'),
                  ),
                  SwitchListTile(
                    contentPadding: EdgeInsets.zero,
                    title: const Text('启用每日整理'),
                    value: enabled,
                    onChanged: (v) => setDialog(() => enabled = v),
                  ),
                ],
              ),
            ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context),
              child: const Text('取消'),
            ),
            FilledButton(
              onPressed: () => Navigator.pop(context, {
                'topics': _split(topics.text),
                'keywords': _split(keywords.text),
                'excluded_keywords': _split(excluded.text),
                'match_all': matchAll,
                'semantic_expansion': old['semantic_expansion'] == true,
                'item_limit': limit,
                'detail_level': detail,
                'send_time': old['send_time'] ?? '08:00',
                'enabled': enabled,
                'notify_enabled': false,
                'version': old['version'] ?? 0,
              }),
              child: const Text('保存'),
            ),
          ],
        ),
      ),
    );
    if (result != null) await _saveSubscription(result);
  }

  Widget _profile() => ListView(
    padding: const EdgeInsets.all(18),
    children: [
      const Text(
        '我的',
        style: TextStyle(
          fontSize: 25,
          fontWeight: FontWeight.w800,
          color: _ink,
        ),
      ),
      const SizedBox(height: 14),
      Card(
        child: ListTile(
          leading: const CircleAvatar(
            backgroundColor: Color(0xFFE8F0EA),
            child: Icon(Icons.person, color: _green),
          ),
          title: Text(
            _user?['display_name']?.toString().isNotEmpty == true
                ? _user!['display_name'].toString()
                : '演示用户',
          ),
          subtitle: Text(_user?['email']?.toString() ?? '本地演示账户'),
        ),
      ),
      Card(
        child: Column(
          children: [
            if (defaultTargetPlatform == TargetPlatform.android)
              SwitchListTile(
                secondary: const Icon(Icons.notifications_active_outlined),
                title: const Text('新简报系统通知'),
                subtitle: const Text('允许 App 在后台检查新简报并发出本机通知；系统可能延迟执行。'),
                value: _backgroundNotificationsEnabled,
                onChanged: _toggleBackgroundNotifications,
              ),
            ListTile(
              leading: const Icon(Icons.dns_outlined),
              title: const Text('服务连接'),
              subtitle: Text(_baseUrl),
              onTap: _openSettings,
            ),
            ListTile(
              leading: const Icon(Icons.account_tree_outlined),
              title: const Text('Agent 运行记录'),
              subtitle: const Text('查看最近任务状态、工具调用和用量'),
              onTap: _showTraces,
            ),
            ListTile(
              leading: const Icon(Icons.feedback_outlined),
              title: const Text('我的反馈'),
              subtitle: const Text('查看举报和更正的处理进度与回复'),
              onTap: _showMyFeedback,
            ),
            if (userHasAdminAccess(_user))
              ListTile(
                leading: const Icon(Icons.admin_panel_settings_outlined),
                title: const Text('举报与更正处理'),
                subtitle: const Text('审核用户提交的问题并回复处理结果'),
                onTap: _showAdminFeedback,
              ),
            ListTile(
              leading: const Icon(Icons.download_outlined),
              title: const Text('复制我的数据'),
              subtitle: const Text('导出账户资料、订阅、简报及相关记录'),
              onTap: _exportMyData,
            ),
            ListTile(
              leading: const Icon(Icons.info_outline),
              title: const Text('关于 PulseBrief'),
              subtitle: const Text('每日 AI 新闻助手'),
            ),
          ],
        ),
      ),
      const SizedBox(height: 12),
      OutlinedButton.icon(
        onPressed: _logout,
        icon: const Icon(Icons.logout),
        label: const Text('退出登录'),
      ),
      const SizedBox(height: 8),
      TextButton.icon(
        onPressed: _deleteAccount,
        icon: const Icon(Icons.delete_forever_outlined, color: Colors.red),
        label: const Text('注销并删除账户', style: TextStyle(color: Colors.red)),
      ),
      const SizedBox(height: 12),
      Text(
        _localLoginEnabled ? '本机体验登录仅限开发环境使用。' : '账户使用邮箱验证码登录；可在“我的”中开启本机系统通知。',
        textAlign: TextAlign.center,
        style: TextStyle(color: Colors.black45, fontSize: 12),
      ),
    ],
  );

  Future<void> _toggleBackgroundNotifications(bool enabled) async {
    if (!enabled) {
      await disableBackgroundNotifications();
      if (mounted) setState(() => _backgroundNotificationsEnabled = false);
      _toast('已关闭新简报系统通知');
      return;
    }
    final token = _token;
    if (token == null) return;
    try {
      final result = await enableBackgroundNotifications(
        baseUrl: _baseUrl,
        token: token,
      );
      if (!mounted) return;
      final enabled = result == BackgroundNotificationEnableResult.enabled;
      setState(() => _backgroundNotificationsEnabled = enabled);
      switch (result) {
        case BackgroundNotificationEnableResult.enabled:
          _toast('已开启。后台检查由 Android 安排，提醒时间可能延迟。');
          break;
        case BackgroundNotificationEnableResult.permissionDenied:
          _toast('未获得通知权限，设置未开启。');
          break;
        case BackgroundNotificationEnableResult.baselineUnavailable:
          _showError('暂时无法读取现有简报，通知未开启。请检查网络后重试。');
          break;
        case BackgroundNotificationEnableResult.unsupportedPlatform:
          _toast('此设备暂不支持本机系统通知。');
          break;
      }
    } catch (_) {
      if (!mounted) return;
      setState(() => _backgroundNotificationsEnabled = false);
      _showError('暂时无法开启系统通知，请稍后重试。');
    }
  }

  Future<void> _showMyFeedback() async {
    try {
      final response = await _api.call('/me/feedback');
      final items = response['items'] as List<dynamic>? ?? [];
      if (!mounted) return;
      await showModalBottomSheet<void>(
        context: context,
        isScrollControlled: true,
        builder: (context) => SafeArea(
          child: SizedBox(
            height: MediaQuery.sizeOf(context).height * 0.75,
            child: Column(
              children: [
                Padding(
                  padding: const EdgeInsets.fromLTRB(20, 20, 20, 12),
                  child: Row(
                    children: [
                      const Expanded(
                        child: Text(
                          '我的反馈',
                          style: TextStyle(
                            fontSize: 20,
                            fontWeight: FontWeight.w700,
                          ),
                        ),
                      ),
                      IconButton(
                        tooltip: '关闭',
                        onPressed: () => Navigator.pop(context),
                        icon: const Icon(Icons.close),
                      ),
                    ],
                  ),
                ),
                Expanded(
                  child: items.isEmpty
                      ? const Center(child: Text('还没有提交过举报或更正。'))
                      : ListView.builder(
                          padding: const EdgeInsets.fromLTRB(16, 0, 16, 20),
                          itemCount: items.length,
                          itemBuilder: (context, index) {
                            final item = Map<String, dynamic>.from(
                              items[index] as Map,
                            );
                            final state = switch (item['status']) {
                              'in_review' => '处理中',
                              'resolved' => '已解决',
                              'dismissed' => '已答复',
                              _ => '待处理',
                            };
                            return Card(
                              margin: const EdgeInsets.only(bottom: 12),
                              child: Padding(
                                padding: const EdgeInsets.all(16),
                                child: Column(
                                  crossAxisAlignment: CrossAxisAlignment.start,
                                  children: [
                                    Text(
                                      item['briefing_title']?.toString() ??
                                          '简报反馈',
                                      style: const TextStyle(
                                        fontWeight: FontWeight.w700,
                                      ),
                                    ),
                                    const SizedBox(height: 4),
                                    Text(
                                      '${item['business_date'] ?? ''} · $state',
                                    ),
                                    const SizedBox(height: 8),
                                    Text(item['detail']?.toString() ?? ''),
                                    if ((item['reply']?.toString() ?? '')
                                        .isNotEmpty) ...[
                                      const Divider(height: 24),
                                      const Text(
                                        '处理回复',
                                        style: TextStyle(
                                          fontWeight: FontWeight.w700,
                                        ),
                                      ),
                                      const SizedBox(height: 4),
                                      Text(item['reply'].toString()),
                                    ],
                                  ],
                                ),
                              ),
                            );
                          },
                        ),
                ),
              ],
            ),
          ),
        ),
      );
    } catch (e) {
      _showError(_cleanError(e));
    }
  }

  Future<void> _showAdminFeedback() async {
    if (!userHasAdminAccess(_user)) {
      _showError('当前账户没有管理员权限。');
      return;
    }
    try {
      final response = await _api.call('/admin/feedback?limit=100');
      final items = (response['items'] as List<dynamic>? ?? [])
          .map((item) => Map<String, dynamic>.from(item as Map))
          .toList();
      if (!mounted) return;
      await showModalBottomSheet<void>(
        context: context,
        isScrollControlled: true,
        builder: (sheetContext) => AdminFeedbackSheet(
          items: items,
          cleanError: _cleanError,
          onSave: (id, status, reply) async => Map<String, dynamic>.from(
            await _api.call(
              '/admin/feedback/${Uri.encodeComponent(id)}',
              method: 'PATCH',
              body: {'status': status, 'reply': reply},
            ) as Map,
          ),
        ),
      );
    } catch (error) {
      _showError('无法读取管理员反馈队列：${_cleanError(error)}');
    }
  }

  Widget _errorView() => ListView(
    padding: const EdgeInsets.all(26),
    children: [
      const SizedBox(height: 100),
      const Icon(Icons.cloud_off_outlined, size: 48, color: Colors.orange),
      const SizedBox(height: 16),
      const Text(
        '暂时连不上服务',
        textAlign: TextAlign.center,
        style: TextStyle(fontWeight: FontWeight.bold, fontSize: 20),
      ),
      const SizedBox(height: 8),
      Text(_error ?? '', textAlign: TextAlign.center),
      const SizedBox(height: 16),
      FilledButton(onPressed: _loadAll, child: const Text('重试')),
      TextButton(onPressed: _openSettings, child: const Text('检查服务地址')),
    ],
  );

  Future<void> _showBriefing(Map<String, dynamic> briefing) async {
    final items = briefing['items'] as List? ?? [];
    if (briefing['id'] is String) {
      _updateBriefing(briefing['id'] as String, read: true);
    }
    await showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      builder: (context) => DraggableScrollableSheet(
        expand: false,
        initialChildSize: .85,
        builder: (context, scroll) => ListView(
          controller: scroll,
          padding: const EdgeInsets.all(20),
          children: [
            Text(
              briefing['title']?.toString() ?? '每日简报',
              style: const TextStyle(fontSize: 23, fontWeight: FontWeight.bold),
            ),
            const SizedBox(height: 6),
            Text(
              '${briefing['business_date'] ?? ''} · ${items.length} 条',
              style: const TextStyle(color: Colors.black54),
            ),
            const SizedBox(height: 16),
            Text(
              briefing['summary']?.toString() ?? '',
              style: const TextStyle(height: 1.6),
            ),
            const SizedBox(height: 16),
            ...items.asMap().entries.map((entry) {
              final itemIndex = entry.key;
              final item = Map<String, dynamic>.from(entry.value as Map);
              final sourceUrl = item['source_url'] ?? item['url'];
              final sourceName = _sourceDisplayName(
                item['source_name'] ?? item['source_id'],
              );
              return Padding(
                padding: const EdgeInsets.only(bottom: 18),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      item['title']?.toString() ?? '',
                      style: const TextStyle(
                        fontSize: 18,
                        fontWeight: FontWeight.bold,
                      ),
                    ),
                    Padding(
                      padding: const EdgeInsets.only(top: 4),
                      child: _tag(
                        item['content_type'] == 'news' ? '新闻' : '专题文章',
                      ),
                    ),
                    if ((item['original_title'] ?? '').toString().isNotEmpty &&
                        item['original_title'] != item['title'])
                      Padding(
                        padding: const EdgeInsets.only(top: 3),
                        child: Text(
                          '原文：${item['original_title']}',
                          style: const TextStyle(
                            fontSize: 12,
                            color: Colors.black54,
                          ),
                        ),
                      ),
                    if ((item['author'] ?? '').toString().trim().isNotEmpty)
                      Padding(
                        padding: const EdgeInsets.only(top: 3),
                        child: Text(
                          '作者：${item['author']}',
                          style: const TextStyle(
                            fontSize: 12,
                            color: Colors.black54,
                          ),
                        ),
                      ),
                    ArticleSourceHeader(
                      sourceName: sourceName,
                      sourceUrl: sourceUrl,
                      onOpenSource: _openSource,
                    ),
                    const SizedBox(height: 6),
                    Text(
                      item['summary']?.toString() ?? '',
                      style: const TextStyle(height: 1.55),
                    ),
                    ArticleAttribution(item: item, onOpenLink: _openSource),
                    if (briefing['id'] is String)
                      TextButton.icon(
                        onPressed: () => _submitBriefingFeedback(
                          briefing['id'] as String,
                          itemIndex,
                        ),
                        icon: const Icon(Icons.flag_outlined, size: 17),
                        label: const Text('举报或提交更正'),
                      ),
                  ],
                ),
              );
            }),
          ],
        ),
      ),
    );
  }

  Future<void> _submitBriefingFeedback(String briefingId, int itemIndex) async {
    var kind = 'incorrect';
    final detail = TextEditingController();
    final submitted = await showDialog<bool>(
      context: context,
      builder: (dialogContext) => StatefulBuilder(
        builder: (context, setDialog) => AlertDialog(
          title: const Text('举报或提交更正'),
          content: SizedBox(
            width: 420,
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                DropdownButtonFormField<String>(
                  initialValue: kind,
                  decoration: const InputDecoration(labelText: '问题类型'),
                  items: const [
                    DropdownMenuItem(
                      value: 'incorrect',
                      child: Text('内容不准确 / 提交更正'),
                    ),
                    DropdownMenuItem(
                      value: 'source_issue',
                      child: Text('来源或链接问题'),
                    ),
                    DropdownMenuItem(
                      value: 'irrelevant',
                      child: Text('与关注主题无关'),
                    ),
                    DropdownMenuItem(value: 'other', child: Text('其他问题')),
                  ],
                  onChanged: (value) => setDialog(() => kind = value ?? kind),
                ),
                const SizedBox(height: 12),
                TextField(
                  controller: detail,
                  minLines: 3,
                  maxLines: 5,
                  maxLength: 1000,
                  decoration: const InputDecoration(
                    labelText: '问题说明或更正内容',
                    hintText: '请描述需要核查或更正的地方',
                    border: OutlineInputBorder(),
                  ),
                ),
              ],
            ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(dialogContext, false),
              child: const Text('取消'),
            ),
            FilledButton(
              onPressed: () => Navigator.pop(dialogContext, true),
              child: const Text('提交'),
            ),
          ],
        ),
      ),
    );
    final message = detail.text.trim();
    detail.dispose();
    if (submitted != true) return;
    if (message.isEmpty) {
      _showError('请填写问题说明或更正内容。');
      return;
    }
    try {
      await _api.call(
        '/briefings/${Uri.encodeComponent(briefingId)}/feedback',
        method: 'POST',
        body: {'item_index': itemIndex, 'kind': kind, 'detail': message},
      );
      _toast('反馈已提交，感谢帮助我们改进简报');
    } catch (e) {
      _showError(_cleanError(e));
    }
  }

  Future<void> _openSource(String value) async {
    final uri = Uri.tryParse(value);
    if (uri == null || uri.scheme != 'https' || uri.host.isEmpty) {
      _showError('这个来源链接无法安全打开。');
      return;
    }
    try {
      if (!await launchUrl(uri, mode: LaunchMode.externalApplication)) {
        _showError('暂时无法打开来源页面。');
      }
    } catch (_) {
      _showError('暂时无法打开来源页面。');
    }
  }

  Future<void> _showTraces() async {
    try {
      final response = await _api.call('/agent/tool-traces?limit=10');
      final tasks = response['items'] as List<dynamic>? ?? [];
      if (!mounted) return;
      await showModalBottomSheet<void>(
        context: context,
        isScrollControlled: true,
        builder: (context) => SafeArea(
          child: SizedBox(
            height: MediaQuery.sizeOf(context).height * .75,
            child: tasks.isEmpty
                ? const Center(child: Text('还没有 Agent 运行记录'))
                : ListView(
                    padding: const EdgeInsets.all(20),
                    children: [
                      const Text(
                        'Agent 运行记录',
                        style: TextStyle(
                          fontSize: 22,
                          fontWeight: FontWeight.bold,
                        ),
                      ),
                      const SizedBox(height: 12),
                      ...tasks.map((raw) {
                        final task = Map<String, dynamic>.from(raw as Map);
                        final calls = task['tools'] as List? ?? [];
                        final usage = task['token_usage'] as Map? ?? {};
                        final requests = usage['requests'] as List? ?? [];
                        return Card(
                          child: ExpansionTile(
                            title: Text('任务：${task['status'] ?? '未知状态'}'),
                            subtitle: Text(
                              '${task['created_at'] ?? ''} · ${calls.length} 次工具调用',
                            ),
                            children: [
                              if (calls.isEmpty)
                                const ListTile(title: Text('没有记录到工具调用')),
                              ...calls.map((rawCall) {
                                final call = Map<String, dynamic>.from(
                                  rawCall as Map,
                                );
                                return ListTile(
                                  dense: true,
                                  leading: const Icon(
                                    Icons.build_outlined,
                                    color: _green,
                                  ),
                                  title: Text(call['tool']?.toString() ?? '工具'),
                                  subtitle: Text(
                                    '${call['ok'] == true ? '成功' : '失败'} · ${call['result_preview']?.toString() ?? ''}',
                                    maxLines: 3,
                                    overflow: TextOverflow.ellipsis,
                                  ),
                                );
                              }),
                              ListTile(
                                dense: true,
                                leading: const Icon(Icons.token_outlined),
                                title: const Text('模型 Token 用量'),
                                subtitle: Text(
                                  '输入 ${usage['prompt'] ?? usage['prompt_tokens'] ?? 0} · 输出 ${usage['completion'] ?? usage['completion_tokens'] ?? 0}',
                                ),
                              ),
                              ...requests.asMap().entries.map((entry) {
                                final request = Map<String, dynamic>.from(
                                  entry.value as Map,
                                );
                                final selectedTools =
                                    (request['tools'] as List? ?? [])
                                        .map((tool) => tool.toString())
                                        .join('、');
                                return ListTile(
                                  dense: true,
                                  leading: const Icon(Icons.analytics_outlined),
                                  title: Text(
                                    '请求 ${entry.key + 1} · ${agentRequestPhaseLabel(request['phase'])}',
                                  ),
                                  subtitle: Text(
                                    '输入 ${request['prompt_tokens'] ?? 0} · 输出 ${request['completion_tokens'] ?? 0}${selectedTools.isEmpty ? '' : ' · 工具：$selectedTools'}',
                                  ),
                                );
                              }),
                            ],
                          ),
                        );
                      }),
                    ],
                  ),
          ),
        ),
      );
    } catch (e) {
      _showError(_cleanError(e));
    }
  }

  Widget _termField(
    TextEditingController controller,
    String label,
    String hint,
  ) => Padding(
    padding: const EdgeInsets.only(bottom: 10),
    child: TextField(
      controller: controller,
      decoration: InputDecoration(
        labelText: label,
        hintText: hint,
        border: const OutlineInputBorder(),
      ),
    ),
  );
  Widget _tag(String label) => Container(
    padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 5),
    decoration: BoxDecoration(
      color: const Color(0xFFE8F0EA),
      borderRadius: BorderRadius.circular(20),
    ),
    child: Text(
      label,
      style: const TextStyle(
        fontSize: 11,
        color: _green,
        fontWeight: FontWeight.w700,
      ),
    ),
  );
  String _sourceDisplayName(Object? value) {
    final source = value?.toString() ?? '';
    const names = {
      'openai-news': 'OpenAI News',
      'github-blog': 'GitHub Blog',
      'inside-java': 'Inside Java',
      'huggingface-blog': 'Hugging Face',
      'arxiv-cs-ai': 'arXiv cs.AI',
      'arxiv-cs-lg': 'arXiv cs.LG',
    };
    return names[source] ?? (source.isEmpty ? '新闻来源' : source);
  }

  List<String> _split(String input) => input
      .split(RegExp('[,，;；\n]'))
      .map((e) => e.trim())
      .where((e) => e.isNotEmpty)
      .toSet()
      .toList();
  String _joined(dynamic value) => value is List ? value.join('，') : '';
  String _dateLabel() {
    final now = DateTime.now();
    return '${now.year}年${now.month}月${now.day}日';
  }

  String _cleanError(Object error) =>
      error.toString().replaceFirst('Exception: ', '');
  void _showError(String message) {
    if (mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(message), backgroundColor: Colors.red.shade700),
      );
    }
  }

  void _toast(String message) {
    if (mounted) {
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(message)));
    }
  }
}

class ArticleAttribution extends StatelessWidget {
  const ArticleAttribution({
    super.key,
    required this.item,
    required this.onOpenLink,
  });

  final Map<String, dynamic> item;
  final ValueChanged<String> onOpenLink;

  @override
  Widget build(BuildContext context) {
    final credit = item['republish_credit']?.toString().trim() ?? '';
    final changeNotice = item['change_notice']?.toString().trim() ?? '';
    final licenseName = item['license_name']?.toString().trim() ?? '';
    final licenseUrl = item['license_url']?.toString().trim() ?? '';
    if (credit.isEmpty && changeNotice.isEmpty && licenseName.isEmpty) {
      return const SizedBox.shrink();
    }
    return Padding(
      padding: const EdgeInsets.only(top: 5),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (credit.isNotEmpty)
            Text(
              credit,
              style: const TextStyle(fontSize: 11, color: Colors.black54),
            ),
          if (changeNotice.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 3),
              child: Text(
                changeNotice,
                style: const TextStyle(fontSize: 11, color: Colors.black54),
              ),
            ),
          if (licenseName.isNotEmpty && licenseUrl.startsWith('https://'))
            TextButton(
              onPressed: () => onOpenLink(licenseUrl),
              style: TextButton.styleFrom(padding: EdgeInsets.zero),
              child: Text('许可：$licenseName'),
            ),
        ],
      ),
    );
  }
}

class ArticleSourceHeader extends StatelessWidget {
  const ArticleSourceHeader({
    super.key,
    required this.sourceName,
    required this.sourceUrl,
    required this.onOpenSource,
  });

  final String sourceName;
  final Object? sourceUrl;
  final ValueChanged<String> onOpenSource;

  @override
  Widget build(BuildContext context) {
    final url = sourceUrl;
    return Padding(
      padding: const EdgeInsets.only(top: 5),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            '来源：$sourceName',
            style: const TextStyle(fontSize: 12, color: _green),
          ),
          if (url is String && url.startsWith('https://'))
            Align(
              alignment: Alignment.centerLeft,
              child: TextButton.icon(
                onPressed: () => onOpenSource(url),
                icon: const Icon(Icons.open_in_new, size: 16),
                label: const Text('打开原文'),
              ),
            ),
        ],
      ),
    );
  }
}

class EmptyBriefingCard extends StatelessWidget {
  const EmptyBriefingCard({
    required this.hasSubscription,
    required this.onEditPreferences,
    super.key,
  });

  final bool hasSubscription;
  final VoidCallback onEditPreferences;

  @override
  Widget build(BuildContext context) => Card(
    child: Padding(
      padding: const EdgeInsets.all(22),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Icon(Icons.auto_awesome, color: _green, size: 28),
          const SizedBox(height: 12),
          Text(
            hasSubscription ? '今天还没有简报' : '你的简报还在等一个关注方向',
            style: const TextStyle(fontSize: 17, fontWeight: FontWeight.bold),
          ),
          const SizedBox(height: 6),
          Text(
            hasSubscription ? '关注偏好已保存。你可以尝试生成预览，或稍后再来查看。' : '先设置关注话题，再生成一份预览。',
            style: const TextStyle(color: Colors.black54),
          ),
          if (!hasSubscription) ...[
            const SizedBox(height: 12),
            OutlinedButton(
              onPressed: onEditPreferences,
              child: const Text('设置订阅偏好'),
            ),
          ],
        ],
      ),
    ),
  );
}

class BriefingNotificationTile extends StatelessWidget {
  const BriefingNotificationTile({
    required this.briefing,
    required this.onTap,
    super.key,
  });

  final Map<String, dynamic> briefing;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final unread = briefing['is_read'] != true;
    final itemCount = (briefing['items'] as List? ?? []).length;
    return Card(
      child: ListTile(
        leading: CircleAvatar(
          backgroundColor: const Color(0xFFE8F0EA),
          child: Icon(
            unread
                ? Icons.notifications_active_outlined
                : Icons.article_outlined,
            color: _green,
          ),
        ),
        title: Text(
          briefing['title']?.toString() ?? '每日简报',
          maxLines: 2,
          overflow: TextOverflow.ellipsis,
        ),
        subtitle: Text('${briefing['business_date'] ?? ''} · $itemCount 条'),
        trailing: unread
            ? const Icon(Icons.circle, size: 10, color: _green)
            : const Icon(Icons.done_all, color: Colors.black45),
        onTap: onTap,
      ),
    );
  }
}
