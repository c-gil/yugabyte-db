// Licensed to the Apache Software Foundation (ASF) under one
// or more contributor license agreements.  See the NOTICE file
// distributed with this work for additional information
// regarding copyright ownership.  The ASF licenses this file
// to you under the Apache License, Version 2.0 (the
// "License"); you may not use this file except in compliance
// with the License.  You may obtain a copy of the License at
//
//   http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing,
// software distributed under the License is distributed on an
// "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
// KIND, either express or implied.  See the License for the
// specific language governing permissions and limitations
// under the License.
//
// The following only applies to changes made to this file as part of YugaByte development.
//
// Portions Copyright (c) YugaByte, Inc.
//
// Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except
// in compliance with the License.  You may obtain a copy of the License at
//
// http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software distributed under the License
// is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
// or implied.  See the License for the specific language governing permissions and limitations
// under the License.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
// http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#include <stdlib.h>
#include <stdio.h>

#include <string>
#include <vector>

#include <boost/utility/binary.hpp>

// Must come before gtest.h.
#include "yb/gutil/mathlimits.h"

#include <glog/logging.h>
#include <gtest/gtest.h>

#include "yb/util/rle-encoding.h"
#include "yb/util/bit-stream-utils.h"
#include "yb/util/hexdump.h"
#include "yb/util/format.h"
#include "yb/util/test_util.h"

using std::string;
using std::vector;

namespace yb {

const int MAX_WIDTH = 32;

class TestRle : public YBTest {};

TEST(BitArray, TestBool) {
  const int len_bytes = 2;
  faststring buffer(len_bytes);

  BitWriter writer(&buffer);

  // Write alternating 0's and 1's
  for (int i = 0; i < 8; ++i) {
    writer.PutValue(i % 2, 1);
  }
  writer.Flush();
  EXPECT_EQ(buffer[0], BOOST_BINARY(1 0 1 0 1 0 1 0));

  // Write 00110011
  for (int i = 0; i < 8; ++i) {
    switch (i) {
      case 0:
      case 1:
      case 4:
      case 5:
        writer.PutValue(0, 1);
        break;
      default:
        writer.PutValue(1, 1);
        break;
    }
  }
  writer.Flush();

  // Validate the exact bit value
  EXPECT_EQ(buffer[0], BOOST_BINARY(1 0 1 0 1 0 1 0));
  EXPECT_EQ(buffer[1], BOOST_BINARY(1 1 0 0 1 1 0 0));

  // Use the reader and validate
  BitReader reader(buffer.data(), buffer.size());
  for (int i = 0; i < 8; ++i) {
    bool val = false;
    bool result = reader.GetValue(1, &val);
    EXPECT_TRUE(result);
    EXPECT_EQ(val, i % 2);
  }

  for (int i = 0; i < 8; ++i) {
    bool val = false;
    bool result = reader.GetValue(1, &val);
    EXPECT_TRUE(result);
    switch (i) {
      case 0:
      case 1:
      case 4:
      case 5:
        EXPECT_EQ(val, false);
        break;
      default:
        EXPECT_EQ(val, true);
        break;
    }
  }
}

// Writes 'num_vals' values with width 'bit_width' and reads them back.
void TestBitArrayValues(int bit_width, int num_vals) {
  const int kTestLen = ceil_div(bit_width * num_vals, 8);
  const uint64_t mod = bit_width == 64? 1 : 1LL << bit_width;

  faststring buffer(kTestLen);
  BitWriter writer(&buffer);
  for (int i = 0; i < num_vals; ++i) {
    writer.PutValue(i % mod, bit_width);
  }
  writer.Flush();
  EXPECT_EQ(writer.bytes_written(), kTestLen);

  BitReader reader(buffer.data(), kTestLen);
  for (int i = 0; i < num_vals; ++i) {
    int64_t val = 0;
    bool result = reader.GetValue(bit_width, &val);
    EXPECT_TRUE(result);
    EXPECT_EQ(val, i % mod);
  }
  EXPECT_EQ(reader.bytes_left(), 0);
}

TEST(BitArray, TestValues) {
  for (int width = 1; width <= MAX_WIDTH; ++width) {
    TestBitArrayValues(width, 1);
    TestBitArrayValues(width, 2);
    // Don't write too many values
    TestBitArrayValues(width, (width < 12) ? (1 << width) : 4096);
    TestBitArrayValues(width, 1024);
  }
}

// Test some mixed values
TEST(BitArray, TestMixed) {
  const int kTestLenBits = 1024;
  faststring buffer(kTestLenBits / 8);
  bool parity = true;

  BitWriter writer(&buffer);
  for (int i = 0; i < kTestLenBits; ++i) {
    if (i % 2 == 0) {
      writer.PutValue(parity, 1);
      parity = !parity;
    } else {
      writer.PutValue(i, 10);
    }
  }
  writer.Flush();

  parity = true;
  BitReader reader(buffer.data(), buffer.size());
  for (int i = 0; i < kTestLenBits; ++i) {
    bool result;
    if (i % 2 == 0) {
      bool val = false;
      result = reader.GetValue(1, &val);
      EXPECT_EQ(val, parity);
      parity = !parity;
    } else {
      int val;
      result = reader.GetValue(10, &val);
      EXPECT_EQ(val, i);
    }
    EXPECT_TRUE(result);
  }
}

// Validates encoding of values by encoding and decoding them.  If
// expected_encoding != NULL, also validates that the encoded buffer is
// exactly 'expected_encoding'.
// if expected_len is not -1, it will validate the encoded size is correct.
template<typename T>
void ValidateRle(const vector<T>& values, int bit_width,
    uint8_t* expected_encoding, int expected_len) {
  faststring buffer;
  RleEncoder<T> encoder(&buffer, bit_width);

  for (const auto& value : values) {
    encoder.Put(value);
  }
  int encoded_len = encoder.Flush();

  if (expected_len != -1) {
    EXPECT_EQ(encoded_len, expected_len);
  }
  if (expected_encoding != nullptr) {
    EXPECT_EQ(memcmp(buffer.data(), expected_encoding, expected_len), 0)
      << "\n"
      << "Expected: " << HexDump(Slice(expected_encoding, expected_len)) << "\n"
      << "Got:      " << HexDump(Slice(buffer));
  }

  // Verify read
  RleDecoder<T> decoder(buffer.data(), encoded_len, bit_width);
  for (const auto value : values) {
    T val = 0;
    bool result = decoder.Get(&val);
    EXPECT_TRUE(result);
    EXPECT_EQ(value, val);
  }
}

TEST(Rle, SpecificSequences) {
  const int kTestLen = 1024;
  uint8_t expected_buffer[kTestLen];
  vector<int> values;

  // Test 50 0' followed by 50 1's
  values.resize(100);
  for (int i = 0; i < 50; ++i) {
    values[i] = 0;
  }
  for (int i = 50; i < 100; ++i) {
    values[i] = 1;
  }

  // expected_buffer valid for bit width <= 1 byte
  expected_buffer[0] = (50 << 1);
  expected_buffer[1] = 0;
  expected_buffer[2] = (50 << 1);
  expected_buffer[3] = 1;
  for (int width = 1; width <= 8; ++width) {
    ValidateRle(values, width, expected_buffer, 4);
  }

  for (int width = 9; width <= MAX_WIDTH; ++width) {
    ValidateRle(values, width, nullptr, 2 * (1 + ceil_div(width, 8)));
  }

  // Test 100 0's and 1's alternating
  for (int i = 0; i < 100; ++i) {
    values[i] = i % 2;
  }
  int num_groups = ceil_div(100, 8);
  expected_buffer[0] = (num_groups << 1) | 1;
  for (int i = 0; i < 100/8; ++i) {
    expected_buffer[i + 1] = BOOST_BINARY(1 0 1 0 1 0 1 0); // 0xaa
  }
  // Values for the last 4 0 and 1's
  expected_buffer[1 + 100/8] = BOOST_BINARY(0 0 0 0 1 0 1 0); // 0x0a

  // num_groups and expected_buffer only valid for bit width = 1
  ValidateRle(values, 1, expected_buffer, 1 + num_groups);
  for (int width = 2; width <= MAX_WIDTH; ++width) {
    ValidateRle(values, width, nullptr, 1 + ceil_div(width * 100, 8));
  }
}

// ValidateRle on 'num_vals' values with width 'bit_width'. If 'value' != -1, that value
// is used, otherwise alternating values are used.
void TestRleValues(int bit_width, int num_vals, int value = -1) {
  const uint64_t mod = (bit_width == 64) ? 1 : 1LL << bit_width;
  vector<int> values;
  for (int v = 0; v < num_vals; ++v) {
    values.push_back((value != -1) ? value : (v % mod));
  }
  ValidateRle(values, bit_width, nullptr, -1);
}

TEST(Rle, TestValues) {
  for (int width = 1; width <= MAX_WIDTH; ++width) {
    TestRleValues(width, 1);
    TestRleValues(width, 1024);
    TestRleValues(width, 1024, 0);
    TestRleValues(width, 1024, 1);
  }
}

class BitRle : public YBTest {
};

// Tests all true/false values
TEST_F(BitRle, AllSame) {
  const int kTestLen = 1024;
  vector<bool> values;

  for (int v = 0; v < 2; ++v) {
    values.clear();
    for (int i = 0; i < kTestLen; ++i) {
      values.push_back(v ? true : false);
    }

    ValidateRle(values, 1, nullptr, 3);
  }
}

// Test that writes out a repeated group and then a literal
// group but flush before finishing.
TEST_F(BitRle, Flush) {
  vector<bool> values;
  for (int i = 0; i < 16; ++i) values.push_back(1);
  values.push_back(false);
  ValidateRle(values, 1, nullptr, -1);
  values.push_back(true);
  ValidateRle(values, 1, nullptr, -1);
  values.push_back(true);
  ValidateRle(values, 1, nullptr, -1);
  values.push_back(true);
  ValidateRle(values, 1, nullptr, -1);
}

// Test some random sequences.
TEST_F(BitRle, Random) {
  int iters = 0;
  const int n_iters = AllowSlowTests() ? 1000 : 20;
  while (iters < n_iters) {
    srand(iters++);
    if (iters % 10000 == 0) LOG(ERROR) << "Seed: " << iters;
    vector<int> values;
    bool parity = 0;
    for (int i = 0; i < 1000; ++i) {
      int group_size = rand() % 20 + 1; // NOLINT(*)
      if (group_size > 16) {
        group_size = 1;
      }
      for (int i = 0; i < group_size; ++i) {
        values.push_back(parity);
      }
      parity = !parity;
    }
    ValidateRle(values, (iters % MAX_WIDTH) + 1, nullptr, -1);
  }
}

// Test a sequence of 1 0's, 2 1's, 3 0's. etc
// e.g. 011000111100000
TEST_F(BitRle, RepeatedPattern) {
  vector<bool> values;
  const int min_run = 1;
  const int max_run = 32;

  for (int i = min_run; i <= max_run; ++i) {
    int v = i % 2;
    for (int j = 0; j < i; ++j) {
      values.push_back(v);
    }
  }

  // And go back down again
  for (int i = max_run; i >= min_run; --i) {
    int v = i % 2;
    for (int j = 0; j < i; ++j) {
      values.push_back(v);
    }
  }

  ValidateRle(values, 1, nullptr, -1);
}

TEST_F(TestRle, TestBulkPut) {
  size_t run_length;
  bool val = false;

  faststring buffer(1);
  RleEncoder<bool> encoder(&buffer, 1);
  encoder.Put(true, 10);
  encoder.Put(false, 7);
  encoder.Put(true, 5);
  encoder.Put(true, 15);
  encoder.Flush();

  RleDecoder<bool> decoder(buffer.data(), encoder.len(), 1);
  run_length = decoder.GetNextRun(&val, MathLimits<size_t>::kMax);
  ASSERT_TRUE(val);
  ASSERT_EQ(10, run_length);

  run_length = decoder.GetNextRun(&val, MathLimits<size_t>::kMax);
  ASSERT_FALSE(val);
  ASSERT_EQ(7, run_length);

  run_length = decoder.GetNextRun(&val, MathLimits<size_t>::kMax);
  ASSERT_TRUE(val);
  ASSERT_EQ(20, run_length);

  ASSERT_EQ(0, decoder.GetNextRun(&val, MathLimits<size_t>::kMax));
}

TEST_F(TestRle, TestGetNextRun) {
  // Repeat the test with different number of items
  for (int num_items = 7; num_items < 200; num_items += 13) {
    SCOPED_TRACE(Format("Num items: $0", num_items));
    // Test different block patterns
    //    1: 01010101 01010101
    //    2: 00110011 00110011
    //    3: 00011100 01110001
    //    ...
    for (int block = 1; block <= 20; ++block) {
      SCOPED_TRACE(Format("Block: $0", block));
      faststring buffer(1);
      RleEncoder<bool> encoder(&buffer, 1);
      for (int j = 0; j < num_items; ++j) {
        encoder.Put(!!(j & 1), block);
      }
      encoder.Flush();

      SCOPED_TRACE(Format("Buffer: $0", Slice(buffer).ToDebugHexString()));
      RleDecoder<bool> decoder(buffer.data(), encoder.len(), 1);
      size_t count = num_items * block;
      for (int j = 0; j < num_items; ++j) {
        SCOPED_TRACE(Format("j: $0", j));
        size_t run_length;
        bool val = false;
        DCHECK_GT(count, 0);
        run_length = decoder.GetNextRun(&val, MathLimits<size_t>::kMax);
        run_length = std::min(run_length, count);

        ASSERT_EQ(!!(j & 1), val);
        ASSERT_EQ(block, run_length);
        count -= run_length;
      }
      DCHECK_EQ(count, 0);
    }
  }
}

// Generate a random bit string which consists of 'num_runs' runs,
// each with a random length between 1 and 100. Returns the number
// of values encoded (i.e the sum run length).
static size_t GenerateRandomBitString(int num_runs, faststring* enc_buf, string* string_rep) {
  RleEncoder<bool> enc(enc_buf, 1);
  int num_bits = 0;
  for (int i = 0; i < num_runs; i++) {
    int run_length = random() % 100;
    bool value = static_cast<bool>(i & 1);
    enc.Put(value, run_length);
    string_rep->append(run_length, value ? '1' : '0');
    num_bits += run_length;
  }
  enc.Flush();
  return num_bits;
}

TEST_F(TestRle, TestRoundTripRandomSequencesWithRuns) {
  SeedRandom();

  // Test the limiting function of GetNextRun.
  const size_t kMaxToReadAtOnce = (random() % 20) + 1;

  // Generate a bunch of random bit sequences, and "round-trip" them
  // through the encode/decode sequence.
  for (int rep = 0; rep < 100; rep++) {
    faststring buf;
    string string_rep;
    auto num_bits = GenerateRandomBitString(10, &buf, &string_rep);
    RleDecoder<bool> decoder(buf.data(), buf.size(), 1);
    string roundtrip_str;
    auto rem_to_read = num_bits;
    size_t run_len;
    bool val = false;
    while (rem_to_read > 0 &&
           (run_len = decoder.GetNextRun(&val, std::min(kMaxToReadAtOnce, rem_to_read))) != 0) {
      ASSERT_LE(run_len, kMaxToReadAtOnce);
      roundtrip_str.append(run_len, val ? '1' : '0');
      rem_to_read -= run_len;
    }

    ASSERT_EQ(string_rep, roundtrip_str);
  }
}
TEST_F(TestRle, TestSkip) {
  faststring buffer(1);
  RleEncoder<bool> encoder(&buffer, 1);

  // 0101010[1] 01010101 01
  //        "A"
  for (int j = 0; j < 18; ++j) {
    encoder.Put(!!(j & 1));
  }

  // 0011[00] 11001100 11001100 11001100 11001100
  //      "B"
  for (int j = 0; j < 19; ++j) {
    encoder.Put(!!(j & 1), 2);
  }

  // 000000000000 11[1111111111] 000000000000 111111111111
  //                   "C"
  // 000000000000 111111111111 0[00000000000] 111111111111
  //                                  "D"
  // 000000000000 111111111111 000000000000 111111111111
  for (int j = 0; j < 12; ++j) {
    encoder.Put(!!(j & 1), 12);
  }
  encoder.Flush();

  bool val = false;
  size_t run_length;
  RleDecoder<bool> decoder(buffer.data(), encoder.len(), 1);

  // position before "A"
  ASSERT_EQ(3, decoder.Skip(7));
  run_length = decoder.GetNextRun(&val, MathLimits<size_t>::kMax);
  ASSERT_TRUE(val);
  ASSERT_EQ(1, run_length);

  // position before "B"
  ASSERT_EQ(7, decoder.Skip(14));
  run_length = decoder.GetNextRun(&val, MathLimits<size_t>::kMax);
  ASSERT_FALSE(val);
  ASSERT_EQ(2, run_length);

  // position before "C"
  ASSERT_EQ(18, decoder.Skip(46));
  run_length = decoder.GetNextRun(&val, MathLimits<size_t>::kMax);
  ASSERT_TRUE(val);
  ASSERT_EQ(10, run_length);

  // position before "D"
  ASSERT_EQ(24, decoder.Skip(49));
  run_length = decoder.GetNextRun(&val, MathLimits<size_t>::kMax);
  ASSERT_FALSE(val);
  ASSERT_EQ(11, run_length);

  encoder.Flush();
}
} // namespace yb
