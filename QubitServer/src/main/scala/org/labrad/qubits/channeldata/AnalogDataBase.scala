package org.labrad.qubits.channeldata

import org.labrad.qubits.channels.AnalogChannel

abstract class AnalogDataBase extends AnalogData {

  private var _channel: AnalogChannel = null

  @volatile private var _isDeconvolved = false

  override def setChannel(channel: AnalogChannel): Unit = {
    this._channel = channel
  }

  protected def getChannel(): AnalogChannel = {
    _channel
  }

  /**
   * Whether this bit of analog data has been deconvolved.
   */
  override def isDeconvolved(): Boolean = {
    _isDeconvolved
  }

  override def invalidate(): Unit = {
    setDeconvolved(false)
  }

  private[channeldata] def setDeconvolved(isDeconvolved: Boolean): Unit = {
    this._isDeconvolved = isDeconvolved
  }
}
