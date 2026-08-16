import { FormEvent, useState } from 'react';

type Props = {
  onSubmit: (question: string) => Promise<void>;
  disabled?: boolean;
  compact?: boolean;
};

export default function ChatComposer({ onSubmit, disabled = false, compact = false }: Props) {
  const [question, setQuestion] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const value = question.trim();
    if (!value || submitting || disabled) return;
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit(value);
      setQuestion('');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法提交问题');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form className={`chat-composer ${compact ? 'chat-composer--compact' : ''}`} onSubmit={submit}>
      <label className="screen-reader-text" htmlFor="book-question">向本书提问</label>
      <textarea
        id="book-question"
        value={question}
        onChange={event => setQuestion(event.target.value)}
        maxLength={4000}
        disabled={disabled || submitting}
        placeholder="针对这本书提出一个问题"
        rows={compact ? 2 : 3}
      />
      <div className="chat-composer-actions">
        {error ? <p className="chat-composer-error" role="alert">{error}</p> : <span />}
        <button className="chat-submit" type="submit" disabled={disabled || submitting || !question.trim()} aria-label="提交问题">
          {submitting ? '发送中' : '提问'}
        </button>
      </div>
    </form>
  );
}
