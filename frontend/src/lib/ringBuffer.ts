/**
 * 固定容量的环形缓冲区（ring buffer / circular buffer）。
 *
 * 为什么用它存遥测：看板标签页可能一开就是好几天，遥测以 5 Hz × N 台机器人的速度源源不断地到来。
 * 如果用普通数组一直 push，内存会无限增长，图表要画的点也越来越多，页面迟早卡死。
 * 环形缓冲区预先分配 capacity 个槽位，写满后新数据直接覆盖最旧的那条：
 *   - 内存上限是确定的（capacity 条），和页面打开多久无关；
 *   - push 是 O(1)：只移动一个下标。对比 array.shift() 删除头部是 O(n)，每次都要把后面所有元素往前挪。
 *
 *   start 指向最旧的元素，length 是当前元素个数，逻辑上第 i 个元素在 items[(start + i) % capacity]。
 */
export class RingBuffer<T> {
  readonly capacity: number;
  private readonly items: Array<T | undefined>;
  private start = 0;
  private length = 0;

  constructor(capacity: number) {
    if (!Number.isInteger(capacity) || capacity <= 0) {
      throw new Error(`RingBuffer capacity must be a positive integer, got ${capacity}`);
    }
    this.capacity = capacity;
    this.items = new Array<T | undefined>(capacity);
  }

  get size(): number {
    return this.length;
  }

  push(item: T): void {
    const end = (this.start + this.length) % this.capacity;
    this.items[end] = item;
    if (this.length < this.capacity) {
      this.length += 1;
    } else {
      // 已满：刚才那一步覆盖了最旧的元素，所以“最旧”的位置往后挪一格
      this.start = (this.start + 1) % this.capacity;
    }
  }

  /** 最旧的元素 */
  first(): T | undefined {
    return this.length > 0 ? this.items[this.start] : undefined;
  }

  /** 最新的元素 */
  last(): T | undefined {
    return this.length > 0 ? this.items[(this.start + this.length - 1) % this.capacity] : undefined;
  }

  /** 按从旧到新的顺序复制出一个普通数组（给 React/ECharts 用的不可变快照） */
  toArray(): T[] {
    const out = new Array<T>(this.length);
    for (let i = 0; i < this.length; i++) {
      out[i] = this.items[(this.start + i) % this.capacity] as T;
    }
    return out;
  }

  clear(): void {
    this.items.fill(undefined);
    this.start = 0;
    this.length = 0;
  }
}
