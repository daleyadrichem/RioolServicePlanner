import { toTagClass } from '../utils/status';

export function Tag({ children }) {
  return <span className={`tag ${toTagClass(children)}`}>{children}</span>;
}
