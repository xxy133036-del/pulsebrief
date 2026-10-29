import 'package:flutter/material.dart';

typedef AdminFeedbackSave = Future<Map<String, dynamic>> Function(
  String id,
  String status,
  String reply,
);

class AdminFeedbackSheet extends StatefulWidget {
  const AdminFeedbackSheet({
    required this.items,
    required this.onSave,
    required this.cleanError,
    super.key,
  });

  final List<Map<String, dynamic>> items;
  final AdminFeedbackSave onSave;
  final String Function(Object error) cleanError;

  @override
  State<AdminFeedbackSheet> createState() => _AdminFeedbackSheetState();
}

class _AdminFeedbackSheetState extends State<AdminFeedbackSheet> {
  late final List<Map<String, dynamic>> _items;
  late final Map<String, String> _statuses;
  late final Map<String, String> _replies;
  final Set<String> _saving = {};

  @override
  void initState() {
    super.initState();
    _items = widget.items.map(Map<String, dynamic>.from).toList();
    _statuses = {
      for (final item in _items)
        item['id'].toString(): item['status']?.toString() ?? 'open',
    };
    _replies = {
      for (final item in _items)
        item['id'].toString(): item['reply']?.toString() ?? '',
    };
  }

  String _statusLabel(Object? status) => switch (status) {
    'in_review' => '处理中',
    'resolved' => '已解决',
    'dismissed' => '不采纳',
    _ => '待处理',
  };

  Future<void> _save(Map<String, dynamic> item) async {
    final id = item['id'].toString();
    final status = _statuses[id] ?? 'open';
    final reply = _replies[id]?.trim() ?? '';
    if ({'resolved', 'dismissed'}.contains(status) && reply.isEmpty) {
      ScaffoldMessenger.of(context)
          .showSnackBar(const SnackBar(content: Text('完成或不采纳反馈前，请填写处理回复。')));
      return;
    }

    setState(() => _saving.add(id));
    try {
      final updated = await widget.onSave(id, status, reply);
      if (!mounted) return;
      final index = _items.indexWhere((row) => row['id'].toString() == id);
      setState(() {
        if (index >= 0) _items[index] = updated;
        _statuses[id] = updated['status']?.toString() ?? 'open';
        _replies[id] = updated['reply']?.toString() ?? '';
      });
      ScaffoldMessenger.of(context)
          .showSnackBar(const SnackBar(content: Text('处理结果已保存。')));
    } catch (error) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('保存失败：${widget.cleanError(error)}')),
      );
    } finally {
      if (mounted) setState(() => _saving.remove(id));
    }
  }

  @override
  Widget build(BuildContext context) => SafeArea(
    child: SizedBox(
      height: MediaQuery.sizeOf(context).height * 0.86,
      child: Column(
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 18, 12, 12),
            child: Row(
              children: [
                const Expanded(
                  child: Text(
                    '举报与更正处理',
                    style: TextStyle(fontSize: 20, fontWeight: FontWeight.w700),
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
            child: _items.isEmpty
                ? const Center(child: Text('当前没有待处理的举报或更正。'))
                : ListView.builder(
                    padding: const EdgeInsets.fromLTRB(16, 0, 16, 24),
                    itemCount: _items.length,
                    itemBuilder: (context, index) {
                      final item = _items[index];
                      final id = item['id'].toString();
                      final saving = _saving.contains(id);
                      final statusLabel = _statusLabel(item['status']);
                      return Card(
                        margin: const EdgeInsets.only(bottom: 12),
                        child: Padding(
                          padding: const EdgeInsets.all(16),
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(
                                item['briefing_title']?.toString() ?? '简报反馈',
                                style: const TextStyle(
                                  fontWeight: FontWeight.w700,
                                ),
                              ),
                              const SizedBox(height: 4),
                              Text(
                                '${item['business_date'] ?? ''} · $statusLabel · ${item['kind'] == 'correction' ? '内容更正' : '问题举报'}',
                                style: const TextStyle(color: Colors.black54),
                              ),
                              const SizedBox(height: 10),
                              Text(item['detail']?.toString() ?? ''),
                              const SizedBox(height: 12),
                              DropdownButtonFormField<String>(
                                key: ValueKey('admin-feedback-status-$id'),
                                initialValue: _statuses[id] ?? 'open',
                                decoration: const InputDecoration(
                                  labelText: '处理状态',
                                ),
                                items: const [
                                  DropdownMenuItem(
                                    value: 'open',
                                    child: Text('待处理'),
                                  ),
                                  DropdownMenuItem(
                                    value: 'in_review',
                                    child: Text('处理中'),
                                  ),
                                  DropdownMenuItem(
                                    value: 'resolved',
                                    child: Text('已解决'),
                                  ),
                                  DropdownMenuItem(
                                    value: 'dismissed',
                                    child: Text('不采纳'),
                                  ),
                                ],
                                onChanged: saving
                                    ? null
                                    : (value) {
                                        if (value != null) {
                                          setState(() => _statuses[id] = value);
                                        }
                                      },
                              ),
                              const SizedBox(height: 8),
                              TextFormField(
                                key: ValueKey('admin-feedback-reply-$id'),
                                initialValue: _replies[id],
                                maxLength: 1000,
                                maxLines: 3,
                                readOnly: saving,
                                decoration: const InputDecoration(
                                  labelText: '给用户的处理回复',
                                  hintText: '说明核查结果或更正情况',
                                  alignLabelWithHint: true,
                                ),
                                onChanged: (value) => _replies[id] = value,
                              ),
                              Align(
                                alignment: Alignment.centerRight,
                                child: FilledButton.icon(
                                  key: ValueKey('admin-feedback-save-$id'),
                                  onPressed: saving ? null : () => _save(item),
                                  icon: saving
                                      ? const SizedBox(
                                          width: 16,
                                          height: 16,
                                          child: CircularProgressIndicator(
                                            strokeWidth: 2,
                                          ),
                                        )
                                      : const Icon(Icons.save_outlined),
                                  label: const Text('保存处理结果'),
                                ),
                              ),
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
  );
}
