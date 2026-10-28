import { Suspense } from 'react';
import PipelinePage from '@/components/pipelines/PipelinePage';

export default function DigitalHumanPipelinePage() {
  return (
    <Suspense fallback={null}>
      <PipelinePage
        pipeline="digital_human"
        title="其他"
        subtitle="更多创作能力正在开发中，敬请期待"
      />
    </Suspense>
  );
}
