import { useEffect, useMemo, useState } from 'react';
import { useParams } from 'react-router-dom';
import {
  Alert,
  Button,
  Col,
  Divider,
  Form,
  Input,
  InputNumber,
  Row,
  Select,
  Space,
  Statistic,
  Typography,
  message,
  theme,
} from 'antd';
import { RocketOutlined, SaveOutlined } from '@ant-design/icons';
import { useStore } from '../store';
import { autoWritingApi, writingStyleApi } from '../services/api';
import { eventBus } from '../store/eventBus';
import type { AutoWritingStartRequest, WritingStyle } from '../types';

const { Text, Title } = Typography;

interface AutoWritingFormValues {
  target_total_words: number;
  chapters_per_batch: number;
  target_words_per_chapter: number;
  overall_threshold: number;
  coherence_threshold: number;
  max_quality_retries: number;
  consecutive_quality_failure_limit: number;
  model?: string;
  style_id?: number | null;
}

export default function AutoWriting() {
  const { projectId } = useParams<{ projectId: string }>();
  const { currentProject, chapters } = useStore();
  const [form] = Form.useForm<AutoWritingFormValues>();
  const [submitting, setSubmitting] = useState(false);
  const [taskId, setTaskId] = useState<string>();
  const [styles, setStyles] = useState<WritingStyle[]>([]);
  const [loadingStyles, setLoadingStyles] = useState(false);
  const { token } = theme.useToken();

  const initialTargetWords = currentProject?.target_words && currentProject.target_words > 0
    ? currentProject.target_words
    : Math.max((currentProject?.current_words || 0) + 50000, 50000);

  const defaultStyleId = useMemo(
    () => styles.find((style) => style.is_default)?.id,
    [styles],
  );

  useEffect(() => {
    form.setFieldsValue({
      target_total_words: initialTargetWords,
      chapters_per_batch: 3,
      target_words_per_chapter: 3000,
      overall_threshold: 7.5,
      coherence_threshold: 7,
      max_quality_retries: 2,
      consecutive_quality_failure_limit: 3,
      style_id: defaultStyleId ?? null,
    });
  }, [defaultStyleId, form, initialTargetWords]);

  useEffect(() => {
    const loadStyles = async () => {
      if (!projectId) return;
      try {
        setLoadingStyles(true);
        const response = await writingStyleApi.getProjectStyles(projectId);
        setStyles(response.styles || []);
      } catch (error) {
        console.error('加载写作风格失败:', error);
      } finally {
        setLoadingStyles(false);
      }
    };

    loadStyles();
  }, [projectId]);

  const handleSubmit = async (values: AutoWritingFormValues) => {
    if (!projectId) {
      message.error('缺少项目 ID');
      return;
    }

    const payload: AutoWritingStartRequest = {
      mode: 'existing_project',
      project_id: projectId,
      target_total_words: values.target_total_words,
      chapters_per_batch: values.chapters_per_batch,
      target_words_per_chapter: values.target_words_per_chapter,
      quality_config: {
        overall_threshold: values.overall_threshold,
        coherence_threshold: values.coherence_threshold,
        max_quality_retries: values.max_quality_retries,
        consecutive_quality_failure_limit: values.consecutive_quality_failure_limit,
      },
      model: values.model?.trim() || undefined,
      style_id: values.style_id ?? null,
    };

    try {
      setSubmitting(true);
      const response = await autoWritingApi.start(payload);
      setTaskId(response.task_id);
      eventBus.emit('background-task-created');
      message.success('自动写作任务已提交');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div style={{ height: '100%', overflow: 'auto' }}>
      <Space direction="vertical" size={18} style={{ width: '100%' }}>
        <div>
          <Space align="center" size={10}>
            <RocketOutlined style={{ color: token.colorPrimary, fontSize: 22 }} />
            <Title level={4} style={{ margin: 0 }}>自动写作</Title>
          </Space>
          <Text type="secondary">
            当前项目：{currentProject?.title || projectId}
          </Text>
        </div>

        <Row gutter={[16, 16]}>
          <Col xs={24} sm={8}>
            <Statistic title="当前字数" value={currentProject?.current_words || 0} suffix="字" />
          </Col>
          <Col xs={24} sm={8}>
            <Statistic title="现有章节" value={chapters.length} suffix="章" />
          </Col>
          <Col xs={24} sm={8}>
            <Statistic title="运行模式" value="现有项目" />
          </Col>
        </Row>

        {taskId && (
          <Alert
            type="success"
            showIcon
            message="任务已创建"
            description={
              <Space direction="vertical" size={4}>
                <Text code>{taskId}</Text>
                <Text type="secondary">可在右下角后台任务面板查看进度。</Text>
              </Space>
            }
          />
        )}

        <Form
          form={form}
          layout="vertical"
          onFinish={handleSubmit}
          requiredMark={false}
          style={{ maxWidth: 980 }}
        >
          <Divider orientation="left" plain>生成范围</Divider>
          <Row gutter={16}>
            <Col xs={24} md={8}>
              <Form.Item
                label="目标总字数"
                name="target_total_words"
                rules={[{ required: true, message: '请输入目标总字数' }]}
              >
                <InputNumber min={1000} step={1000} precision={0} addonAfter="字" style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item
                label="每批章节数"
                name="chapters_per_batch"
                rules={[{ required: true, message: '请输入每批章节数' }]}
              >
                <InputNumber min={1} max={20} precision={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item
                label="单章目标字数"
                name="target_words_per_chapter"
                rules={[{ required: true, message: '请输入单章目标字数' }]}
              >
                <InputNumber min={500} step={500} precision={0} addonAfter="字" style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>

          <Divider orientation="left" plain>质量门槛</Divider>
          <Row gutter={16}>
            <Col xs={24} md={6}>
              <Form.Item
                label="整体质量阈值"
                name="overall_threshold"
                rules={[{ required: true, message: '请输入整体质量阈值' }]}
              >
                <InputNumber min={0} max={10} step={0.1} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={24} md={6}>
              <Form.Item
                label="连贯性阈值"
                name="coherence_threshold"
                rules={[{ required: true, message: '请输入连贯性阈值' }]}
              >
                <InputNumber min={0} max={10} step={0.1} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={24} md={6}>
              <Form.Item
                label="最大质量重试"
                name="max_quality_retries"
                rules={[{ required: true, message: '请输入最大质量重试' }]}
              >
                <InputNumber min={0} max={10} precision={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={24} md={6}>
              <Form.Item
                label="连续质量失败停止阈值"
                name="consecutive_quality_failure_limit"
                rules={[{ required: true, message: '请输入停止阈值' }]}
              >
                <InputNumber min={1} max={20} precision={0} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>

          <Divider orientation="left" plain>生成选项</Divider>
          <Row gutter={16}>
            <Col xs={24} md={12}>
              <Form.Item label="模型" name="model">
                <Input placeholder="留空使用当前设置中的默认模型" allowClear />
              </Form.Item>
            </Col>
            <Col xs={24} md={12}>
              <Form.Item label="风格 ID（可选）" name="style_id">
                <Select
                  allowClear
                  showSearch
                  loading={loadingStyles}
                  placeholder="可选择项目写作风格，也可留空"
                  optionFilterProp="label"
                  options={styles.map((style) => ({
                    value: style.id,
                    label: style.is_default ? `${style.name}（默认）` : style.name,
                  }))}
                />
              </Form.Item>
            </Col>
          </Row>

          <Form.Item style={{ marginBottom: 0 }}>
            <Button
              type="primary"
              htmlType="submit"
              icon={<SaveOutlined />}
              loading={submitting}
            >
              启动自动写作
            </Button>
          </Form.Item>
        </Form>
      </Space>
    </div>
  );
}
