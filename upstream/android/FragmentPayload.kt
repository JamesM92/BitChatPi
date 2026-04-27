// upstream snapshot — DO NOT EDIT
// source: app/src/main/java/com/bitchat/android/model/FragmentPayload.kt
// commit: 9795e2ce8a8fe01181d537f4254da3c6631e126e  (2025-08-18)
// fetched: 2026-04-25
package com.bitchat.android.model

import com.bitchat.android.protocol.MessageType

/**
 * FragmentPayload - 100% iOS-compatible fragment payload structure
 *
 * Fragment payload structure (matching iOS):
 * - 8 bytes: Fragment ID (random bytes)
 * - 2 bytes: Index (big-endian)
 * - 2 bytes: Total count (big-endian)
 * - 1 byte: Original message type
 * - Variable: Fragment data
 *
 * Total header size: 13 bytes
 */
data class FragmentPayload(
    val fragmentID: ByteArray,      // 8 bytes - random fragment identifier
    val index: Int,                 // Fragment index (0-based)
    val total: Int,                 // Total number of fragments
    val originalType: UByte,        // Original message type before fragmentation
    val data: ByteArray             // Fragment data
) {

    companion object {
        const val HEADER_SIZE = 13
        const val FRAGMENT_ID_SIZE = 8

        fun decode(payloadData: ByteArray): FragmentPayload? {
            if (payloadData.size < HEADER_SIZE) {
                return null
            }
            try {
                val fragmentID = payloadData.sliceArray(0..<FRAGMENT_ID_SIZE)
                val index = ((payloadData[8].toInt() and 0xFF) shl 8) or
                           (payloadData[9].toInt() and 0xFF)
                val total = ((payloadData[10].toInt() and 0xFF) shl 8) or
                           (payloadData[11].toInt() and 0xFF)
                val originalType = payloadData[12].toUByte()
                val data = if (payloadData.size > HEADER_SIZE) {
                    payloadData.sliceArray(HEADER_SIZE..<payloadData.size)
                } else {
                    ByteArray(0)
                }
                return FragmentPayload(fragmentID, index, total, originalType, data)
            } catch (e: Exception) {
                return null
            }
        }

        fun generateFragmentID(): ByteArray {
            val fragmentID = ByteArray(FRAGMENT_ID_SIZE)
            kotlin.random.Random.nextBytes(fragmentID)
            return fragmentID
        }
    }

    fun encode(): ByteArray {
        val payload = ByteArray(HEADER_SIZE + data.size)
        System.arraycopy(fragmentID, 0, payload, 0, FRAGMENT_ID_SIZE)
        payload[8] = ((index shr 8) and 0xFF).toByte()
        payload[9] = (index and 0xFF).toByte()
        payload[10] = ((total shr 8) and 0xFF).toByte()
        payload[11] = (total and 0xFF).toByte()
        payload[12] = originalType.toByte()
        if (data.isNotEmpty()) {
            System.arraycopy(data, 0, payload, HEADER_SIZE, data.size)
        }
        return payload
    }

    fun getFragmentIDString(): String = fragmentID.joinToString("") { "%02x".format(it) }

    fun isValid(): Boolean =
        fragmentID.size == FRAGMENT_ID_SIZE &&
        index >= 0 &&
        total > 0 &&
        index < total &&
        data.isNotEmpty()
}
