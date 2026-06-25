import { toTagClass, toTagLabel } from '../utils/status';

export function Tag({ children }) {
  return <span className={`tag ${toTagClass(children)}`}>{toTagLabel(children)}</span>;
}
