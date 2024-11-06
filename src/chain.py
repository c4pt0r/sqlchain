#! /usr/bin/env python3
# 
# @Author: dongxu
# @Date:   2024-11-06 10:00:00
# SQLChain
# A chain of SQL operations with parallel processing support
from typing import (
    Iterator, List, Optional, Any, Dict, Type, Callable, TypeVar, Generic, 
    Tuple, Iterable, AsyncIterator, Union, Sequence
)
from dataclasses import make_dataclass, dataclass, field
from datetime import datetime, date
import sqlalchemy as sa
from sqlalchemy.engine import Engine, Connection
from sqlalchemy.sql import text
from contextlib import contextmanager
import decimal
from itertools import groupby
from functools import reduce as functools_reduce
import asyncio
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import multiprocessing as mp
from abc import ABC, abstractmethod
import logging
from enum import Enum
import warnings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
formatter = logging.Formatter('%(asctime)s | %(levelname)s | %(filename)s:%(lineno)d | %(message)s')
handler = logging.StreamHandler()
handler.setFormatter(formatter)
logger.addHandler(handler)


# Type variables
T = TypeVar('T')
R = TypeVar('R')
K = TypeVar('K')

class StreamError(Exception):
    """Base exception for stream operations"""
    pass

class ParallelExecutionError(StreamError):
    """Exception raised when parallel execution fails"""
    pass

class SQLExecutionError(StreamError):
    """Exception raised when SQL execution fails"""
    pass

@dataclass
class ParallelConfig:
    """Configuration for parallel processing"""
    chunk_size: int = 1000
    num_workers: int = 1
    timeout: Optional[float] = None
    retry_count: int = 3
    retry_delay: float = 1.0

@dataclass
class ExecutionStats:
    """Statistics for stream execution"""
    processed_items: int = 0
    failed_items: int = 0
    execution_time: float = 0.0
    error_count: int = 0

class BaseStream(Generic[T], ABC):
    """Base class for stream operations"""
    
    def __init__(
        self,
        source: Iterator[T],
        transforms: List[Callable[[Iterator[Any]], Iterator[Any]]] = None,
        stats: Optional[ExecutionStats] = None
    ) -> None:
        self.source = source
        self.transforms = transforms or []
        self.stats = stats or ExecutionStats()
        self._cached_results: Optional[List[T]] = None

    @abstractmethod
    def map(self, func: Callable[[T], R]) -> 'BaseStream[R]':
        """Transform elements using the provided function"""
        pass

    @abstractmethod
    def filter(self, predicate: Callable[[T], bool]) -> 'BaseStream[T]':
        """Filter elements using the provided predicate"""
        pass

    def _execute(self) -> Iterator[T]:
        """Execute all transformations in the chain"""
        try:
            result: Iterator[Any] = self.source
            for transform in self.transforms:
                result = transform(result)
            return result
        except Exception as e:
            raise StreamError(f"Error executing stream: {str(e)}") from e

    async def collect_async(self) -> List[T]:
        """Collect all elements into a list asynchronously"""
        if self._cached_results is None:
            self._cached_results = list(self._execute())
        return self._cached_results

class Stream(BaseStream[T]):
    """Sequential stream processor"""
    
    def map(self, func: Callable[[T], R]) -> 'Stream[R]':
        """Transform elements using the provided function"""
        return Stream(
            source=self.source,
            transforms=self.transforms + [lambda it: map(func, it)],
            stats=self.stats
        )

    def filter(self, predicate: Callable[[T], bool]) -> 'Stream[T]':
        """Filter elements using the provided predicate"""
        return Stream(
            source=self.source,
            transforms=self.transforms + [lambda it: filter(predicate, it)],
            stats=self.stats
        )

    def group_by(self, key: Callable[[T], K]) -> 'Stream[Tuple[K, List[T]]]':
        """Group elements by key function"""
        def transform(items: Iterable[T]) -> Iterator[Tuple[K, List[T]]]:
            sorted_items = sorted(items, key=key)
            return ((k, list(g)) for k, g in groupby(sorted_items, key=key))
            
        return Stream(
            source=self.source,
            transforms=self.transforms + [transform],
            stats=self.stats
        )

    def parallel(
        self, 
        num_workers: Optional[int] = None,
        chunk_size: Optional[int] = None,
        timeout: Optional[float] = None
    ) -> 'ParallelStream[T]':
        """Switch to parallel processing mode"""
        config = ParallelConfig(
            num_workers=num_workers or 1,
            chunk_size=chunk_size or 1000,
            timeout=timeout
        )
        
        return ParallelStream(
            source=self.source,
            transforms=self.transforms,
            config=config,
            stats=self.stats
        )

    def collect(self) -> List[T]:
        """Collect all elements into a list"""
        return list(self._execute())

class ParallelStream(BaseStream[T]):
    """Parallel stream processor"""
    
    def __init__(
        self,
        source: Iterator[T],
        transforms: List[Callable[[Iterator[Any]], Iterator[Any]]],
        config: ParallelConfig,
        stats: Optional[ExecutionStats] = None
    ) -> None:
        super().__init__(source, transforms, stats)
        self.config = config

    def _chunk_data(self, data: List[T]) -> Iterator[List[T]]:
        """Split data into chunks for parallel processing"""
        for i in range(0, len(data), self.config.chunk_size):
            yield data[i:i + self.config.chunk_size]

    @staticmethod
    def _parallel_map_wrapper(args: Tuple[Callable[[T], R], List[T]]) -> List[R]:
        """Wrapper function for parallel map to avoid pickle errors"""
        func, chunk = args
        return [func(item) for item in chunk]

    @staticmethod
    def _parallel_filter_wrapper(args: Tuple[Callable[[T], bool], List[T]]) -> List[T]:
        """Wrapper function for parallel filter to avoid pickle errors"""
        predicate, chunk = args
        return [item for item in chunk if predicate(item)]

    async def _retry_operation(
        self,
        operation: Callable,
        args: Any,
        retry_count: int,
        retry_delay: float
    ) -> Any:
        """Helper function to handle retries with proper async sleep"""
        while retry_count > 0:
            try:
                return operation(args)
            except Exception as e:
                retry_count -= 1
                if retry_count > 0:
                    warnings.warn(f"Operation failed, retrying: {str(e)}")
                    await asyncio.sleep(retry_delay)
                else:
                    raise

    def map(self, func: Callable[[T], R]) -> 'ParallelStream[R]':
        """Transform elements in parallel"""
        def transform(it: Iterator[T]) -> Iterator[R]:
            data = list(it)
            chunks = list(self._chunk_data(data))
            
            # Prepare args for each chunk
            chunk_args = [(func, chunk) for chunk in chunks]
            
            with ThreadPoolExecutor(max_workers=self.config.num_workers) as executor:
                logger.info(f"num_workers: {self.config.num_workers}")
                try:
                    # Process chunks in parallel using the wrapper function
                    chunk_results = executor.map(
                        self._parallel_map_wrapper,
                        chunk_args,
                        timeout=self.config.timeout
                    )
                    
                    # Process results
                    for chunk_result in chunk_results:
                        yield from chunk_result
                        self.stats.processed_items += len(chunk_result)
                        
                except Exception as e:
                    self.stats.error_count += 1
                    raise ParallelExecutionError(f"Map operation failed: {str(e)}") from e

        return ParallelStream(
            source=self.source,
            transforms=self.transforms + [transform],
            config=self.config,
            stats=self.stats
        )

    def filter(self, predicate: Callable[[T], bool]) -> 'ParallelStream[T]':
        def transform(it: Iterator[T]) -> Iterator[T]:
            data = list(it)
            chunks = list(self._chunk_data(data))
            
            # Prepare args for each chunk
            chunk_args = [(predicate, chunk) for chunk in chunks]
            
            with ThreadPoolExecutor(max_workers=self.config.num_workers) as executor:
                try:
                    # Process chunks in parallel using the wrapper function
                    chunk_results = executor.map(
                        self._parallel_filter_wrapper,
                        chunk_args,
                        timeout=self.config.timeout
                    )
                    
                    # Process results
                    for chunk_result in chunk_results:
                        yield from chunk_result
                        self.stats.processed_items += len(chunk_result)
                        
                except Exception as e:
                    self.stats.error_count += 1
                    raise ParallelExecutionError(f"Filter operation failed: {str(e)}") from e

        return ParallelStream(
            source=self.source,
            transforms=self.transforms + [transform],
            config=self.config,
            stats=self.stats
        )

    @staticmethod
    def _parallel_reduce_wrapper(args: Tuple[Callable[[T, T], T], List[T]]) -> T:
        """Wrapper function for parallel reduce to avoid pickle errors"""
        func, chunk = args
        return functools_reduce(func, chunk)

    async def reduce_async(
        self, 
        func: Callable[[T, T], T], 
        initial: Optional[T] = None
    ) -> T:
        """Reduce elements asynchronously in parallel"""
        data = list(self._execute())
        if not data:
            raise ValueError("Cannot reduce empty sequence")
            
        chunks = list(self._chunk_data(data))
        chunk_args = [(func, chunk) for chunk in chunks]
        
        loop = asyncio.get_event_loop()
        with ProcessPoolExecutor(max_workers=self.config.num_workers) as executor:
            try:
                # Create tasks for parallel processing
                tasks = [
                    loop.run_in_executor(
                        executor,
                        self._parallel_reduce_wrapper,
                        args
                    )
                    for args in chunk_args
                ]
                
                # Wait for all tasks to complete
                chunk_results = await asyncio.gather(*tasks)
                self.stats.processed_items = len(data)
                
                # Final reduction
                if initial is not None:
                    return functools_reduce(func, chunk_results, initial)
                return functools_reduce(func, chunk_results)
                
            except Exception as e:
                self.stats.error_count += 1
                raise ParallelExecutionError(f"Reduce operation failed: {str(e)}") from e

class SQLChain:
    TYPE_MAPPING: Dict[str, Type] = {
        'int': int,
        'bigint': int,
        'tinyint': int,
        'smallint': int,
        'mediumint': int,
        'float': float,
        'double': float,
        'decimal': decimal.Decimal,
        'char': str,
        'varchar': str,
        'text': str,
        'datetime': datetime,
        'timestamp': datetime,
        'date': date,
        'time': str,
        'boolean': bool,
        'bool': bool,
    }

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._type_cache: Dict[str, Type] = {}

    def _create_dynamic_class(self, result_metadata: List[sa.engine.RowMapping]) -> Type:
        """Create a typed dataclass from query result metadata"""
        if not result_metadata:
            raise ValueError("No metadata available for creating dynamic class")

        fields: List[Tuple[str, Type, None]] = []
        for key, value in result_metadata[0].items():
            python_type = type(value) if value is not None else str
            fields.append((key, python_type, None))

        # Create the dataclass in the global namespace to make it picklable
        DynamicRecord = make_dataclass(
            cls_name='DynamicRecord',
            fields=fields,
            frozen=True,
            namespace={'__module__': '__main__'}
        )
        
        # Register the class in the global namespace
        import sys
        setattr(sys.modules[__name__], 'DynamicRecord', DynamicRecord)
        
        return DynamicRecord

    @contextmanager
    def get_connection(self) -> Iterator[Connection]:
        """Get database connection"""
        with self.engine.connect() as connection:
            yield connection

    def query(
        self, 
        sql: str, 
        params: Optional[Dict[str, Any]] = None
    ) -> Stream[Any]:
        """Execute query and return result stream"""
        def result_generator() -> Iterator[Any]:
            try:
                with self.get_connection() as conn:
                    result = conn.execute(text(sql), params or {})
                    rows = result.mappings().all()
                    
                    if not rows:
                        return

                    # Convert rows to dictionaries first
                    for row in rows:
                        row_dict = dict(row)
                        # Convert any non-serializable types to strings
                        for key, value in row_dict.items():
                            if isinstance(value, (datetime, date, decimal.Decimal)):
                                row_dict[key] = str(value)
                        yield row_dict  # Yield dictionary instead of DynamicRecord

            except Exception as e:
                raise SQLExecutionError(f"Query execution failed: {str(e)}") from e

        return Stream(source=result_generator())
 
async def example_usage() -> None:
    engine = sa.create_engine(
        "mysql+pymysql://user:password@host:port/database",
        pool_size=5,
        max_overflow=10
    )
    
    try:
        chain = SQLChain(engine)
        result = await (chain
            .query("SELECT * FROM hncrawler.feeds limit 100")
            .parallel(
                num_workers=10, 
                chunk_size=5,
                timeout=10
            )
            .map(lambda f: f['link']) 
            .filter(lambda link: link is not None and link.startswith('http://') )
            .collect_async()
        )
        print(result)

    except StreamError as e:
        logger.error(f"Stream processing error: {str(e)}")
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
    finally:
        engine.dispose()

if __name__ == "__main__":
    asyncio.run(example_usage())