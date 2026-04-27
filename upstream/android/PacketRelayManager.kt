// upstream snapshot — DO NOT EDIT
// source: app/src/main/java/com/bitchat/android/mesh/PacketRelayManager.kt
// commit: 66012e9fe954c603dcf3e31ad62202159a337a92  (2026-01-12)
// fetched: 2026-04-25
package com.bitchat.android.mesh
import com.bitchat.android.protocol.MessageType

import android.util.Log
import com.bitchat.android.model.RoutedPacket
import com.bitchat.android.protocol.BitchatPacket
import com.bitchat.android.util.toHexString
import kotlinx.coroutines.*
import kotlin.random.Random

/**
 * Centralized packet relay management
 */
class PacketRelayManager(private val myPeerID: String) {
    private val debugManager by lazy { try { com.bitchat.android.ui.debug.DebugSettingsManager.getInstance() } catch (e: Exception) { null } }

    companion object {
        private const val TAG = "PacketRelayManager"
    }

    private fun isRelayEnabled(): Boolean = try {
        com.bitchat.android.ui.debug.DebugSettingsManager.getInstance().packetRelayEnabled.value
    } catch (_: Exception) { true }

    var delegate: PacketRelayManagerDelegate? = null

    private val relayScope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    suspend fun handlePacketRelay(routed: RoutedPacket) {
        val packet = routed.packet
        val peerID = routed.peerID ?: "unknown"

        if (isPacketAddressedToMe(packet)) return
        if (peerID == myPeerID) return

        if (packet.ttl == 0u.toUByte()) return

        val relayPacket = packet.copy(ttl = (packet.ttl - 1u).toUByte())

        // Source-based routing
        val route = relayPacket.route
        if (!route.isNullOrEmpty()) {
            if (route.map { it.toHexString() }.toSet().size < route.size) return // duplicate hops
            val myIdBytes = hexStringToPeerBytes(myPeerID)
            val index = route.indexOfFirst { it.contentEquals(myIdBytes) }
            if (index >= 0) {
                val nextHopIdHex: String? = run {
                    val nextIndex = index + 1
                    if (nextIndex < route.size) route[nextIndex].toHexString()
                    else relayPacket.recipientID?.toHexString()
                }
                if (nextHopIdHex != null) {
                    val success = try { delegate?.sendToPeer(nextHopIdHex, RoutedPacket(relayPacket, peerID, routed.relayAddress)) } catch (_: Exception) { false } ?: false
                    if (success) return
                }
            }
        }

        val shouldRelay = isRelayEnabled() && shouldRelayPacket(relayPacket, peerID)
        if (shouldRelay) {
            delegate?.broadcastPacket(RoutedPacket(relayPacket, peerID, routed.relayAddress))
        }
    }

    internal fun isPacketAddressedToMe(packet: BitchatPacket): Boolean {
        val recipientID = packet.recipientID ?: return false
        val broadcastRecipient = delegate?.getBroadcastRecipient()
        if (broadcastRecipient != null && recipientID.contentEquals(broadcastRecipient)) return false
        return recipientID.toHexString() == myPeerID
    }

    // IMPORTANT: relay probability tiers — keep in sync with Pi relay_engine.py
    private fun shouldRelayPacket(packet: BitchatPacket, fromPeerID: String): Boolean {
        if (packet.ttl >= 4u) return true          // high TTL: always relay

        val networkSize = delegate?.getNetworkSize() ?: 1

        if (networkSize <= 3) return true           // tiny network: always relay

        val relayProb = when {
            networkSize <= 10  -> 1.0               // small:   100%
            networkSize <= 30  -> 0.85              // medium:   85%
            networkSize <= 50  -> 0.7               // moderate: 70%
            networkSize <= 100 -> 0.55              // large:    55%
            else               -> 0.4              // huge:     40%
        }

        return Random.nextDouble() < relayProb
    }

    fun shutdown() { relayScope.cancel() }
}

interface PacketRelayManagerDelegate {
    fun getNetworkSize(): Int
    fun getBroadcastRecipient(): ByteArray
    fun broadcastPacket(routed: RoutedPacket)
    fun sendToPeer(peerID: String, routed: RoutedPacket): Boolean
}

private fun hexStringToPeerBytes(hex: String): ByteArray {
    val result = ByteArray(8)
    var idx = 0; var out = 0
    while (idx + 1 < hex.length && out < 8) {
        result[out++] = hex.substring(idx, idx + 2).toIntOrNull(16)?.toByte() ?: 0
        idx += 2
    }
    return result
}
